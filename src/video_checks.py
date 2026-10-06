"""Filename-only reuse and explicit, local, cancellable video verification.

Normal planning reads metadata only. Full verification is a separate operation;
its failure records live on the system disk and survive later quick runs.
"""

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from pathlib import Path

from platformdirs import user_data_path

from .artifacts import atomic_write_json, internal_path

BAD_STATUSES = {"invalid", "failed", "needs_review", "corrupt", "incomplete"}
MEDIA_ERROR = re.compile(
    r"IncompleteMediaError|无法读取媒体|录像未下载完整|录像文件结构|媒体校验失败"
)


def state_root():
    return Path(
        os.environ.get("ICOURSE_STATE_DIR")
        or user_data_path("Fudan iCourse", appauthor=False)
    )


def record_path(path):
    key = hashlib.sha256(str(Path(path).absolute()).encode()).hexdigest()
    return state_root() / "video-checks" / (key + ".json")


def signature(path):
    s = Path(path).stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def has_symlink(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    try:
        relative = path.relative_to(root)
        current = root
        if current.is_symlink():
            return True
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                return True
        return False
    except (ValueError, OSError):
        return True


def safe_regular(path, root):
    path = Path(path)
    try:
        return (
            not has_symlink(path, root)
            and stat.S_ISREG(path.lstat().st_mode)
            and path.stat().st_size > 0
        )
    except OSError:
        return False


def read_marker(path):
    """Read only bounded metadata; never follow a marker symlink."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return None
    if (
        path.is_symlink()
        or path.parent.is_symlink()
        or not path.is_file()
        or path.stat().st_size > 65536
    ):
        raise ValueError("异常校验记录")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("异常校验记录")
    return value


def metadata(path):
    legacy = Path(str(path) + ".icourse.json")
    marker = internal_path(legacy)
    return read_marker(marker if marker.exists() or marker.is_symlink() else legacy)


def previous_failure(path, course_id, sub_id, since=0):
    db = state_root() / "pipeline.sqlite3"
    if not db.is_file():
        return ""
    connection = None
    try:
        connection = sqlite3.connect(db.as_uri() + "?mode=ro", timeout=1)
        rows = connection.execute(
            "SELECT status,error,payload,updated_at FROM pipeline_jobs WHERE course_id=? AND sub_id=? "
            "AND stage IN ('download','transcribe') AND status IN ('done','failed') ORDER BY id DESC LIMIT 20",
            (str(course_id), str(sub_id)),
        )
        for status, error, payload, updated in rows:
            if updated <= since:
                continue
            target = (json.loads(payload) or {}).get("target_video_path")
            if target and Path(target).absolute() == Path(path).absolute():
                if status == "done":
                    return ""
                if MEDIA_ERROR.search(error or ""):
                    return "已有录像失败记录，请先完整校验。"

    except (sqlite3.Error, ValueError, OSError):
        # An unreadable history cannot be treated as proof of a healthy video.
        return "无法读取历史校验状态，请先完整校验。"
    finally:
        if connection:
            connection.close()
    return ""


def quick_status(path, root, course_id="", sub_id=""):
    """Return an explanatory error or empty string; no media bytes are read."""
    if not safe_regular(path, root):
        return "已有录像为空、不是普通文件或路径含符号链接，未复用。"
    try:
        record = read_marker(record_path(path))
        verified = bool(
            record
            and record.get("status") == "verified"
            and record.get("signature") == signature(path)
        )
        full_verified = bool(verified and record.get("method") == "manual"
                             and re.fullmatch(r"[0-9a-f]{64}", record.get("sha256") or ""))
        if record and record.get("status") in BAD_STATUSES:
            return (
                "已有完整校验失败记录：" + str(record.get("reason", "请检查文件"))[:160]
            )
        legacy = Path(str(path) + ".icourse.json")
        marker_path = internal_path(legacy)
        if not marker_path.exists() and not marker_path.is_symlink():
            marker_path = legacy
        marker_newer = marker_path.exists() and (
            not verified or marker_path.stat().st_mtime > record.get("checked_at", 0)
        )
        try:
            marker = metadata(path)
            if marker and marker.get("status") in BAD_STATUSES and marker_newer:
                return "已有录像被标记异常，请先完整校验。"
        except (ValueError, OSError, UnicodeError):
            if not full_verified or marker_newer:
                return "已有录像的校验记录损坏，请先完整校验。"
    except (ValueError, OSError, UnicodeError):
        return "已有录像的校验记录损坏，请先完整校验。"
    return (
        previous_failure(
            path, course_id, sub_id, record.get("checked_at", 0) if verified else 0
        )
        if course_id and sub_id
        else ""
    )


def candidates(root, course_ids, sub_ids=()):
    """Only ID-bearing formal .mp4 files in selected course folders."""
    root = Path(root).absolute()
    wanted = set(map(str, course_ids))
    subs = set(map(str, sub_ids))
    if not root.is_dir() or root.is_symlink():
        return
    for folder in sorted(root.iterdir()):
        match = re.fullmatch(r"(\d+)[-_](.+)", folder.name)
        if not match or match[1] not in wanted or not folder.is_dir():
            continue
        # Never traverse a symlinked course directory.
        if folder.is_symlink():
            continue
        for path in sorted(folder.rglob("*.mp4")):
            if path.name.startswith("._") or ".icourse" in path.relative_to(folder).parts:
                continue
            ident = re.search(r"_(\d{5,})$", path.stem) or re.match(
                r"^(\d{5,})(?:[_-].*)?$", path.stem
            )
            if ident and (not subs or ident[1] in subs):
                yield match[1], ident[1], path


def find_video(root, course_id, sub_id):
    root = Path(root).absolute()
    if root.is_symlink():
        return root, "录像目录是符号链接，未复用或写入。"
    if root.is_dir():
        for folder in root.iterdir():
            if (
                re.fullmatch(re.escape(str(course_id)) + r"[-_].+", folder.name)
                and folder.is_symlink()
            ):
                return folder, "课程目录是符号链接，未复用或写入。"
    found = [p for _, _, p in candidates(root, [course_id], [sub_id])]
    if not found:
        return None, ""
    if len(found) != 1:
        return found[0], "同一课次有多个录像文件，请先核对，未自动选择。"
    path = found[0]
    return path, quick_status(path, root, course_id, sub_id)


def remember(path, status, reason="", digest=None, method=None):
    if status != "verified" and digest is None:
        try:
            prior = read_marker(record_path(path)) or {}
            digest = prior.get("sha256")
        except (ValueError, OSError, UnicodeError):
            pass
    atomic_write_json(
        record_path(path),
        dict(
            schema=1,
            status=status,
            reason=reason,
            signature=signature(path),
            sha256=digest,
            method=method,
            checked_at=time.time(),
        ),
        private=True,
    )


def verify_video(path, root, *, cancel=None, progress=None):
    """Read every byte, compare known hash and probe; never touch the video."""
    from .media import CancelledError, probe
    from .video_storage import uncached_reader

    path = Path(path)

    def cancelled():
        if cancel is not None and cancel.is_set():
            raise CancelledError("校验已取消；原文件及异常记录保持不变。")

    cancelled()
    if not safe_regular(path, root):
        raise ValueError("仅能校验所选课程目录内的非空普通录像，不能跟随符号链接。")
    before = signature(path)
    digest = hashlib.sha256()
    complete = 0
    try:
        try:
            trusted = read_marker(record_path(path)) or {}
        except (ValueError, OSError, UnicodeError):
            trusted = {}
        with uncached_reader(path) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                cancelled()
                digest.update(chunk)
                complete += len(chunk)
                if progress:
                    progress(complete, before[2])
        cancelled()
        probe(path, cancel=cancel)
        cancelled()
        if signature(path) != before:
            raise RuntimeError("校验期间文件发生变化，请稍后重试。")
        try:
            expected = metadata(path)
        except (ValueError, OSError, UnicodeError):
            expected = None  # Damaged metadata is preserved; full verification can supersede it locally.
        actual = digest.hexdigest()
        if trusted.get("sha256") and trusted["sha256"] != actual:
            raise RuntimeError("录像内容与本机保存的原接收摘要不一致。")
        if expected and expected.get("sha256") and expected["sha256"] != actual:
            raise RuntimeError("录像内容与原校验值不一致。")
        remember(path, "verified", digest=actual, method="manual")
        return actual
    except CancelledError:
        raise
    except Exception as exc:
        remember(path, "invalid", str(exc)[:200])
        raise


def main(argv=None):
    import argparse

    from filelock import FileLock, Timeout

    from . import task_events as events

    parser = argparse.ArgumentParser(
        description="仅完整校验本地录像，不登录、不下载、不调用云服务"
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--course-ids", required=True)
    parser.add_argument("--sub-ids", default="")
    args = parser.parse_args(argv)
    args.out_dir = args.out_dir.expanduser().absolute()
    ids = [x.strip() for x in args.course_ids.split(",")]
    if not all(re.fullmatch(r"\d+", i) for i in ids):
        parser.error("课程 ID 必须为数字，以英文逗号分隔")
    state_root().mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(state_root() / "pipeline.lock", timeout=0):
            items = list(
                candidates(args.out_dir, ids, filter(None, args.sub_ids.split(",")))
            )
            tasks = []
            for course, sub, path in items:
                task = dict(
                    course_id=course,
                    sub_id=sub,
                    course_title=path.relative_to(args.out_dir).parts[0],
                    sub_title=path.stem,
                    target_video_path=path,
                )
                # Duplicate identities cannot be represented as independent UI tasks.
                if any(t["course_id"] == course and t["sub_id"] == sub for t in tasks):
                    continue
                tasks.append(task)
                events.plan_task(task, dict(dl="queued", tr="na", sm="na"), dl=path)
            events.emit("planned", total=len(tasks))
            failed = 0
            for task in tasks:
                events.bind_task(task, "dl")
                events.stage(task, "dl", "running", "完整校验（不下载）")
                last = 0.0

                def progress(done, total):
                    nonlocal last
                    now = time.monotonic()
                    if done == total or now - last >= 0.25:
                        events.progress(
                            "完整校验本地录像",
                            phase="verify",
                            unit="bytes",
                            completed=done,
                            total=total,
                        )
                        last = now

                try:
                    _, problem = find_video(
                        args.out_dir, task["course_id"], task["sub_id"]
                    )
                    if problem.startswith("同一课次"):
                        raise ValueError(problem)
                    verify_video(
                        task["target_video_path"], args.out_dir, progress=progress
                    )
                    events.stage(task, "dl", "done", "完整校验通过")
                    print(f"[校验通过] {task['course_id']}/{task['sub_id']}")
                except Exception as exc:
                    failed += 1
                    events.stage(task, "dl", "failed", events.redact(str(exc)))
                    print(
                        f"[校验失败] {task['course_id']}/{task['sub_id']}：{events.redact(str(exc))}"
                    )
            events.emit("run_finished", status="failed" if failed else "done")
            return int(bool(failed))
    except Timeout:
        print("另一个课程任务正在运行，请等待完成后再校验。")
        return 2
