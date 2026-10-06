import argparse
import contextlib
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__
from .api import CanvasClient, ConnectionFailure, LoginRequired
from .config import load_config
from .state import Store, run_lock
from .sync import clean, run_sync

ROOT = Path(__file__).resolve().parent
STATUS = {"unsubmitted": "未提交", "submitted": "已提交", "pending_review": "已提交，待评阅",
          "graded": "已评分", "excused": "免交", "unknown": "提交状态未确认",
          "no_online_submission": "无需在线提交"}
FILE_STATUS = {"downloaded": "新增", "duplicate": "重复跳过", "skipped": "跳过",
               "failed": "失败", "would_download": "计划下载/比对", "would_check_content": "计划内容比对",
               "local_changed": "保留本地变化", "name_preserved": "同名保留"}


def due(value, tz):
    if value is None:
        return "未设置截止时间"
    return datetime.fromisoformat(value).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")


def display(results, tz):
    for result in results:
        print(f"\n{clean(result['course'])}（{result['course_id']}）")
        for error in result["errors"]:
            print("  错误：" + error)
        checked = result.get("assignments")
        if checked:
            # Presentation only: JSON results, baseline and alert history stay complete.
            visible = [item for item in checked["assignments"] if not item.get("locked_for_user")]
            visible_ids = {item["id"] for item in visible}
            events = [event for event in checked["alerts"] if event.get("id") in visible_ids]
            print(f"  作业清单（{len(visible)} 项）" + ("（预览，不更新基线）" if checked["status"] == "preview" else ""))
            for item in visible:
                print(f"    {item['title']} | {due(item['due_at'], tz)} | {STATUS[item['submission']]}")
                print("      " + item["url"])
            if checked["first_run"]:
                print("  首次建立基线；以上为当前可见作业，不逐条通知历史作业。")
            elif not events:
                print("  本次可显示作业没有新增或截止时间变化。")
            for event in events:
                if event["kind"] == "new_assignment":
                    print(f"  [新作业] {event['title']}：{due(event['due_at'], tz)}")
                elif event["kind"] == "deadline_changed":
                    print(f"  [截止时间变更] {event['title']}：{due(event['old_due_at'], tz)} → {due(event['due_at'], tz)}")
        for item in result["files"]:
            detail = item.get("reason") or item.get("path") or item.get("note") or ""
            if item["status"] in {"local_changed", "name_preserved"}:
                detail += "；" + item["note"]
                if not item.get("downloaded_bytes"):
                    detail += "；未下载课件"
                if item.get("downloaded_bytes"):
                    detail += f"；临时内容比对已下载 {item['downloaded_bytes']:,} 字节"
            if item["status"] == "duplicate":
                transferred = item.get("downloaded_bytes", 0)
                detail += (f"；临时内容比对已下载 {transferred:,} 字节，未新增副本" if transferred
                           else "；未下载课件")
            if item.get("version_unconfirmed"):
                detail += "；来源缺少版本时间，未认定远端版本不变"
            if item.get("download_attempts", 1) > 1:
                detail += f"；第 {item['download_attempts']} 次完整下载校验通过"
            size = f" ({item['bytes']:,} 字节)" if "bytes" in item else ""
            print(f"  [{FILE_STATUS[item['status']]}] {item['name']}{size} {detail}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="复旦 eLearning：课程文件同步与新作业提醒（手动运行）")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json", help="不含秘密的课程与路径配置")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果，不输出认证信息")
    parser.add_argument("--login-stdin", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="离线检查配置和路径，不读取凭据、不联网、不创建目录")
    commands.add_parser("demo", help="用临时模拟数据演示去重和作业变化，不访问学校")
    for command in ("sync", "check"):
        child = commands.add_parser(command, help="同步文件并检查作业" if command == "sync" else "仅检查作业")
        child.add_argument("--dry-run", action="store_true", help="只读学校清单与本地索引，不下载、不更新索引或提醒")
        if command == "sync":
            child.add_argument("--compare-existing", action="store_true",
                               help=argparse.SUPPRESS)
    alert = commands.add_parser("alerts", help="查看本地未读提醒，不联网")
    alert.add_argument("--all", action="store_true", help="也显示已读提醒")
    ack = commands.add_parser("ack", help="将指定的本地提醒标为已读")
    ack.add_argument("ids", type=int, nargs="+")
    args = parser.parse_args(argv)
    store = None
    client = None
    school_session = None
    try:
        if args.command == "demo":
            from .demo import run_demo
            result = run_demo()
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print("离线演示：第一轮、产生新作业及延期的第二轮、无变化的第三轮")
                for key in ("first_sync", "second_sync", "third_sync"):
                    print("\n" + key)
                    display(result[key], "Asia/Shanghai")
                print("\n" + result["note"])
            return 0
        config = load_config(args.config)
        if args.command == "doctor":
            report = {"version": __version__, "max_bytes": config.max_bytes,
                      "authentication": "未验证；doctor 不读取环境中的凭据、浏览器或钥匙串",
                      "runtime_auth_variable": config.auth_env,
                      "courses": [{"id": c.id, "name": c.name, "directory": str(c.directory),
                                   "exists": c.directory.is_dir(), "symlink": c.directory.is_symlink(),
                                   "dedup_directories": [str(c.directory)]}
                                  for c in config.courses],
                      "state_dir": str(config.state_dir), "scheduler_enabled": False,
                      "notifications": "每次手动运行时的终端提醒，以及 alerts 未读列表"}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.command in {"alerts", "ack"}:
            if args.command == "ack" and not (config.state_dir / "index.sqlite3").exists():
                raise ValueError("尚无本地提醒索引")
            with run_lock(config.state_dir) if args.command == "ack" else contextlib.nullcontext():
                store = Store(config.state_dir / "index.sqlite3", readonly=args.command == "alerts")
                if args.command == "ack":
                    store.acknowledge(args.ids)
                    print("指定提醒已标为已读（仅本地）。")
                else:
                    rows = store.alerts(include_read=args.all)
                    for row in rows:
                        row["body"] = json.loads(row["body"])
                    print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if args.login_stdin:
            # A private parent/child pipe, never argv, environment, a file or Keychain.
            # Only the GUI's current explicit user submission enters this branch.
            from .auth import MemorySchoolSession
            raw = sys.stdin.buffer.read(8193)
            if len(raw) > 8192:
                raise LoginRequired("登录输入过长，未发送到学校。")
            try:
                credentials = json.loads(raw)
                if (not isinstance(credentials, dict) or set(credentials) != {"student_id", "password"}
                        or not all(isinstance(value, str) for value in credentials.values())):
                    raise ValueError("invalid credential input")
            except (ValueError, UnicodeError):
                raise LoginRequired("未收到有效的一次性登录输入，请在应用中重新提交。") from None
            finally:
                del raw
            school_session = MemorySchoolSession(config.base_url)
            try:
                school_session.login(credentials["student_id"], credentials["password"])
            finally:
                credentials.clear()
                del credentials
            print("eLearning 登录已通过校验；开始本次课程检查。", file=sys.stderr)
            client = CanvasClient(config.base_url, download_hosts=config.download_hosts, session=school_session)
        else:
            # Existing CLI token mode remains optional; GUI never selects it.
            credential = os.environ.get(config.auth_env, "")
            client = CanvasClient(config.base_url, credential, config.download_hosts)
            del credential
        with contextlib.nullcontext() if args.dry_run else run_lock(config.state_dir):
            store = Store(config.state_dir / "index.sqlite3", readonly=args.dry_run)
            results = run_sync(client, store, config, dry_run=args.dry_run, assignments_only=args.command == "check",
                               compare_existing=getattr(args, "compare_existing", False))
        if args.json:
            print(json.dumps({"dry_run": args.dry_run, "results": results}, ensure_ascii=False, indent=2))
        else:
            if args.dry_run:
                print("预览模式：不会下载课件、写入索引或发出持久提醒；同名直接保留，异名文件按内容去重。")
            display(results, config.timezone)
        return 1 if any(r["errors"] or any(f["status"] == "failed" for f in r["files"]) for r in results) else 0
    except LoginRequired as exc:
        print("需要登录/授权：" + str(exc), file=sys.stderr)
        print("未保存凭据，也未将本次失败记为检查成功。已有成功步骤仍保留，可重试。", file=sys.stderr)
        return 3
    except ConnectionFailure as exc:
        print("连接失败：" + str(exc), file=sys.stderr)
        return 1
    except (ValueError, KeyError, OSError, sqlite3.Error, RuntimeError) as exc:
        # Do not render raw server responses or request objects containing authorization data.
        print(f"无法完成：{type(exc).__name__}。请检查配置、路径权限或是否已有进程运行。", file=sys.stderr)
        if isinstance(exc, (ValueError, RuntimeError)) and not isinstance(exc, json.JSONDecodeError):
            print(clean(str(exc)), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中止；已完成文件保留，未完成文件不会发布，可重新运行。", file=sys.stderr)
        return 130
    finally:
        if store:
            store.close()
        if client:
            client.close()
        elif school_session:
            school_session.close()


if __name__ == "__main__":
    raise SystemExit(main())
