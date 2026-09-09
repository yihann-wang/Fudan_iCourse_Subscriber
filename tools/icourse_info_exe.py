#!/usr/bin/env python3
"""Interactive iCourse info fetcher (ready for PyInstaller packaging)."""

from __future__ import annotations

import getpass
import json
import os
import sys
from datetime import datetime
from pathlib import Path


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    text = f"{label}{suffix}: "
    if secret:
        raw = getpass.getpass(text)
    else:
        raw = input(text)
    raw = raw.strip()
    return raw if raw else default


def _prompt_required(label: str, default: str = "", secret: bool = False) -> str:
    while True:
        value = _prompt(label, default=default, secret=secret)
        if value:
            return value
        print("输入不能为空，请重试。")


def _prompt_yes_no(label: str, default_yes: bool = True) -> bool:
    default = "Y/n" if default_yes else "y/N"
    while True:
        raw = input(f"{label} [{default}]: ").strip().lower()
        if not raw:
            return default_yes
        if raw in ("y", "yes", "1"):
            return True
        if raw in ("n", "no", "0"):
            return False
        print("请输入 y 或 n。")


def _collect_inputs() -> dict:
    env = os.environ
    print("=== iCourse 课程信息获取工具 ===")
    print("提示：仅用于个人学习，请遵守学校平台使用规范。")
    print()

    student_id = _prompt_required("学号 StuId", env.get("StuId", ""))
    password = _prompt_required("UIS 密码 UISPsw", env.get("UISPsw", ""), secret=True)
    course_ids_raw = _prompt_required(
        "课程编号 COURSE_IDS（逗号分隔）",
        env.get("COURSE_IDS", ""),
    )
    course_ids = _parse_csv(course_ids_raw)

    print()
    print("=== 可选：LLM 配置（用于后续总结，获取课程信息本身不依赖）===")
    api_key = _prompt("LLM_API_KEY_1", env.get("LLM_API_KEY_1", ""), secret=True)
    base_url = _prompt("LLM_BASE_URL_1", env.get("LLM_BASE_URL_1", env.get("LLM_BASE_URL", "")))
    models = _prompt("LLM_MODELS_1", env.get("LLM_MODELS_1", env.get("LLM_MODELS", "gpt-5.4")))
    provider_name = _prompt("LLM_NAME_1", env.get("LLM_NAME_1", "provider1"))

    save_env = _prompt_yes_no("是否保存本次输入到 .env.generated", default_yes=True)
    return {
        "StuId": student_id,
        "UISPsw": password,
        "COURSE_IDS": ",".join(course_ids),
        "course_ids": course_ids,
        "LLM_API_KEY_1": api_key,
        "LLM_BASE_URL_1": base_url,
        "LLM_MODELS_1": models,
        "LLM_NAME_1": provider_name,
        "save_env": save_env,
    }


def _write_generated_env(project_root: Path, cfg: dict) -> Path:
    out_path = project_root / ".env.generated"
    lines = [
        f"StuId={cfg['StuId']}",
        f"UISPsw={cfg['UISPsw']}",
        f"COURSE_IDS={cfg['COURSE_IDS']}",
        f"LLM_NAME_1={cfg['LLM_NAME_1']}",
        f"LLM_API_KEY_1={cfg['LLM_API_KEY_1']}",
        f"LLM_BASE_URL_1={cfg['LLM_BASE_URL_1']}",
        f"LLM_MODELS_1={cfg['LLM_MODELS_1']}",
    ]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def _set_runtime_env(cfg: dict) -> None:
    os.environ["StuId"] = cfg["StuId"]
    os.environ["UISPsw"] = cfg["UISPsw"]
    os.environ["COURSE_IDS"] = cfg["COURSE_IDS"]
    os.environ["LLM_NAME_1"] = cfg["LLM_NAME_1"]
    os.environ["LLM_API_KEY_1"] = cfg["LLM_API_KEY_1"]
    os.environ["LLM_BASE_URL_1"] = cfg["LLM_BASE_URL_1"]
    os.environ["LLM_MODELS_1"] = cfg["LLM_MODELS_1"]


def _format_lecture_line(lecture: dict) -> str:
    sub_id = lecture.get("sub_id", "")
    sub_title = lecture.get("sub_title", "")
    date = lecture.get("date", "")
    has_playback = "是" if lecture.get("has_playback") else "否"
    return f"    - [{sub_id}] {sub_title} ({date}) 回放: {has_playback}"


def _fetch_course_info(project_root: Path, course_ids: list[str]) -> tuple[list[dict], list[dict]]:
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from src.icourse import ICourseClient  # pylint: disable=import-error
    from src.webvpn import WebVPNSession  # pylint: disable=import-error

    vpn = WebVPNSession()
    print()
    print("正在登录 WebVPN...")
    vpn.login()
    print("正在登录 iCourse...")
    vpn.authenticate_icourse()
    client = ICourseClient(vpn)

    success = []
    failed = []
    for course_id in course_ids:
        print()
        print(f"[Course] {course_id}")
        try:
            detail = client.get_course_detail(course_id)
            lectures = detail.get("lectures", [])
            playback_lectures = [lec for lec in lectures if lec.get("has_playback")]
            print(f"  标题: {detail.get('title', '')}")
            print(f"  教师: {detail.get('teacher', '')}")
            print(f"  总课次: {len(lectures)}")
            print(f"  有回放课次: {len(playback_lectures)}")
            for lecture in lectures:
                print(_format_lecture_line(lecture))
            success.append(
                {
                    "course_id": course_id,
                    "title": detail.get("title", ""),
                    "teacher": detail.get("teacher", ""),
                    "lectures_total": len(lectures),
                    "lectures_playback": len(playback_lectures),
                    "lectures": lectures,
                }
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  获取失败: {type(exc).__name__}: {exc}")
            failed.append(
                {
                    "course_id": course_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

    return success, failed


def _write_result_json(project_root: Path, success: list[dict], failed: list[dict]) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = project_root / f"course_info_{ts}.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "success_count": len(success),
        "failed_count": len(failed),
        "courses": success,
        "failed": failed,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    os.chdir(project_root)

    try:
        cfg = _collect_inputs()
        _set_runtime_env(cfg)

        if cfg["save_env"]:
            env_path = _write_generated_env(project_root, cfg)
            print(f"已保存配置到: {env_path}")

        success, failed = _fetch_course_info(project_root, cfg["course_ids"])
        result_path = _write_result_json(project_root, success, failed)
        print()
        print("=== 完成 ===")
        print(f"成功课程数: {len(success)}")
        print(f"失败课程数: {len(failed)}")
        print(f"结果文件: {result_path}")
        return 0 if not failed else 2
    except KeyboardInterrupt:
        print("\n用户取消。")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"\n运行失败: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
