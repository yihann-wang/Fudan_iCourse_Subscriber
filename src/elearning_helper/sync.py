import hashlib
import http.client
import json
import os
import re
import stat
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .api import ConnectionFailure, LoginRequired
from .config import HARD_LIMIT, excluded_local_directory


class FileRejected(RuntimeError):
    pass


DOWNLOAD_RETRY_DELAYS = (1, 2)


class RetryableDownload(FileRejected):
    def __init__(self, reason, response, expected, received):
        super().__init__(reason + "；" + download_diagnostic(response, expected, received))
        self.received = received


def clean(text):
    return "".join(ch for ch in str(text) if not unicodedata.category(ch).startswith("C"))[:240]


def identifier(value):
    value = str(value)
    if not value.isdecimal():
        raise ConnectionFailure("学校返回了无效的对象 ID")
    return value


def safe_name(value):
    name = unicodedata.normalize("NFC", clean(value))
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip(" .")
    if not name or name.startswith("."):
        raise FileRejected("文件名不可用")
    # Stay under macOS's byte-based filename limit, reserving room for versions.
    stem, ext = os.path.splitext(name)
    while len(ext.encode("utf-8")) > 40:
        ext = ext[:-1]
    while len(stem.encode("utf-8")) > 160:
        stem = stem[:-1]
    return stem + ext.lower()


def sha256(path):
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise FileRejected("候选课件不是普通文件，未读取")
        for block in iter(lambda: stream.read(256 * 1024), b""):
            digest.update(block)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise FileRejected("比对时本地课件发生变化，未采用该结果")
    return digest.hexdigest()


def version(file):
    return json.dumps([file.get("modified_at") or file.get("updated_at"), file["size"]], separators=(",", ":"))


def eligible(file, course, config, folders=None):
    if (file.get("locked_for_user") or file.get("hidden_for_user") or file.get("locked")
            or file.get("published") is False or file.get("workflow_state") == "unpublished"):
        return "未发布或当前不可访问"
    size = file.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return "缺少可靠大小或大小无效"
    if size >= min(config.max_bytes, HARD_LIMIT):
        return "达到或超过大小上限（必须严格小于 50,000,000 字节）"
    return None


def reject_login_document(path):
    """Ordinary HTML is a file; only recognizable school login forms are refused."""
    with Path(path).open("rb") as stream:
        head = stream.read(128 * 1024).decode("utf-8", "replace")
    if not head.lstrip().lower().startswith(("<!doctype html", "<html")):
        return
    password = re.search(r"<input\b[^>]*\btype\s*=\s*[\"']?password\b", head, re.I)
    school_login = re.search(r"复旦.{0,30}统一身份认证|id\.fudan\.edu\.cn/(?:idp|ac)/|<title[^>]*>[^<]*(?:Canvas|eLearning)[^<]*(?:登录|log[ -]?in|sign[ -]?in)", head, re.I)
    if password and school_login and re.search(r"<form\b", head, re.I):
        raise LoginRequired("下载响应为学校登录表单，未保存；请亲自重新登录")


def safe_directory(path):
    path = Path(path)
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise FileRejected("保存目录包含符号链接，需明确实际保存位置")
    if path.exists() and not path.is_dir():
        raise FileRejected("保存位置不是目录")


def local_candidates(directories, size, extension=None, exclude_patterns=()):
    """Metadata-only walk of approved courseware roots, with unrelated trees pruned."""
    if isinstance(directories, (str, Path)):
        directories = (Path(directories),)
    seen = set()
    for root in directories:
        root = Path(root)
        safe_directory(root)
        if not root.exists():
            continue
        def walk_error(error):
            raise FileRejected("无法完整检查已配置的本地课件目录，未继续下载") from None
        for current, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            directory = Path(current)
            safe_directory(directory)
            dirs[:] = sorted(d for d in dirs if not excluded_local_directory(d) and not (directory / d).is_symlink())
            for name in sorted(files):
                candidate = directory / name
                if (candidate in seen or name.startswith(".") or (extension is not None and candidate.suffix.lower() != extension)
                        or candidate.is_symlink() or not candidate.is_file()):
                    continue
                seen.add(candidate)
                if size is None or candidate.stat().st_size == size:
                    yield candidate


def find_duplicate(directories, digest, size, extension, exclude_patterns=()):
    for candidate in local_candidates(directories, size, extension, exclude_patterns):
        if sha256(candidate) == digest:
            return candidate
    return None


class LocalNamePreserved(Exception):
    def __init__(self, path):
        self.path = path


def same_name_file(directories, name):
    normalized = unicodedata.normalize("NFC", name).casefold()
    for candidate in local_candidates(directories, None, None):
        if (unicodedata.normalize("NFC", candidate.name).casefold() == normalized
                and stat.S_ISREG(candidate.lstat().st_mode)):
            return candidate
    return None


def name_preserved(file, name, path, transferred=0):
    return {"status": "name_preserved", "name": name, "path": str(path),
            "bytes": path.lstat().st_size, "downloaded_bytes": transferred,
            "source_file_id": str(file["id"]), "source_version": version(file), "source_bytes": file["size"],
            "local_content_verified": False,
            "note": "保留同名本地文件，未认定与远端内容相同"}


def publish_without_overwrite(staged, directory, name):
    """Atomic publication; a concurrent same-name file is preserved, never versioned."""
    candidate = directory / name
    try:
        os.link(staged, candidate)
    except FileExistsError:
        if stat.S_ISREG(candidate.lstat().st_mode):
            raise LocalNamePreserved(candidate) from None
        raise FileRejected("同名目标不是普通文件，未覆盖或另存") from None
    return candidate


def download_diagnostic(response, expected, received):
    """Only bounded numeric fields and known protocol tokens; never URLs/cookies."""
    status = getattr(response, "status", None)
    status = status if type(status) is int and 100 <= status <= 599 else "未知"
    length = response.headers.get("Content-Length")
    length = length if isinstance(length, str) and re.fullmatch(r"[0-9]{1,12}", length) else "缺失/无效"
    def encoding(header, default, allowed):
        value = response.headers.get(header, default).strip().lower()
        return value if value in allowed else "其他/无效"
    content = encoding("Content-Encoding", "identity", {"identity", "gzip", "br", "deflate"})
    transfer = encoding("Transfer-Encoding", "none", {"none", "identity", "chunked"})
    return (f"清单={expected}，实收={received}，HTTP={status}，Content-Length={length}，"
            f"Content-Encoding={content}，Transfer-Encoding={transfer}")


def download_attempt(client, directory, url, size, limit):
    """One complete GET, with a fresh temporary file and no Range resumption."""
    descriptor, temp_name = tempfile.mkstemp(prefix=".elearning-", suffix=".part", dir=directory)
    staged = Path(temp_name)
    digest = hashlib.sha256()
    written = 0
    try:
        with os.fdopen(descriptor, "wb") as output, client.open(url, download=True) as response:
            def reject(reason, received=None):
                raise FileRejected(reason + "；" + download_diagnostic(response, size, written if received is None else received))
            status = getattr(response, "status", None)
            if (status is not None and status != 200) or response.headers.get("Content-Range") is not None:
                reject("响应不是完整文件，未读取正文")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                reject("拒绝传输编码改变大小的响应")
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared = int(declared)
                except ValueError:
                    reject("下载响应大小字段无效")
                if declared >= limit:
                    reject("下载响应达到或超过大小上限，未读取正文")
                if declared != size:
                    reject("列表大小与下载响应不一致，未读取正文")
            # HTTPResponse.read(n) can lose a partially received chunk inside
            # IncompleteRead. read1 returns available bytes before the next read,
            # so interruption diagnostics account for the bytes already received.
            read = response.read1 if isinstance(response, http.client.HTTPResponse) else response.read
            while True:
                try:
                    block = read(min(256 * 1024, limit - written))
                except http.client.IncompleteRead as exc:
                    raise RetryableDownload("响应中途截断，未保存课件", response, size, written + len(exc.partial)) from None
                except (OSError, http.client.HTTPException):
                    raise RetryableDownload("传输中断或超时，未保存课件", response, size, written) from None
                if not block:
                    break
                written += len(block)
                if written >= limit:
                    reject("实际下载达到或超过大小上限，未保存课件")
                output.write(block)
                digest.update(block)
            if written < size:
                raise RetryableDownload("实际字节数与学校清单不符，未保存不完整课件", response, size, written)
            if written > size:
                reject("实际字节数超过学校清单，未保存课件")
            output.flush()
            os.fsync(output.fileno())
        reject_login_document(staged)
        return staged, written, digest.hexdigest()
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def download_complete(client, directory, url, size, limit):
    """Retry only confirmed body truncation/interruption; never login or policy errors."""
    transferred = 0
    attempts = len(DOWNLOAD_RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        try:
            staged, written, checksum = download_attempt(client, directory, url, size, limit)
            return staged, written, checksum, transferred + written, attempt
        except RetryableDownload as exc:
            transferred += exc.received
            detail = f"attempt={attempt}/{attempts}；{exc}"
            if attempt == attempts:
                raise FileRejected("下载重试已用尽；" + detail) from None
            delay = DOWNLOAD_RETRY_DELAYS[attempt - 1]
            print(f"下载未完整，将在 {delay} 秒后从头重试；{detail}", file=sys.stderr, flush=True)
            time.sleep(delay)  # SIGINT/KeyboardInterrupt propagates; cancellation is never retried.


def sync_file(client, store, config, course, file, *, dry_run, compare_existing=False):
    fid = identifier(file["id"])
    name = safe_name(file.get("display_name") or file.get("filename"))
    size = file["size"]
    limit = min(config.max_bytes, HARD_LIMIT)
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size < limit:
        raise FileRejected("文件大小必须严格小于大小上限，未开始下载")
    safe_directory(course.directory)
    search_dirs = (course.directory,)  # Never reuse legacy or additional roots.
    for directory in search_dirs:
        safe_directory(directory)
    # Same-name preservation takes precedence over source versions and hashes.
    # The legacy compare_existing argument cannot opt out of the user's policy.
    matching = same_name_file(search_dirs, name)
    if matching:
        return name_preserved(file, name, matching)
    target = course.directory / name
    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(target_stat.st_mode):
            raise FileRejected("同名目标不是普通文件，未下载或改动")
        return name_preserved(file, name, target)
    old = store.file(course.id, fid)
    version_known = bool(file.get("modified_at") or file.get("updated_at"))
    if old and version_known and old["version"] == version(file):
        # Search only approved roots, never trust an arbitrary cached filesystem
        # path. This also recovers a renamed/moved local copy without a download.
        duplicate = find_duplicate(search_dirs, old["sha256"], size, None)
        if duplicate:
            if not dry_run and str(duplicate) != old["path"]:
                store.save_file(course.id, fid, version(file), size, old["sha256"], duplicate)
            return {"status": "duplicate", "name": name, "path": str(duplicate), "bytes": size,
                    "downloaded_bytes": 0, "note": "来源版本与本地内容一致，未下载"}
        # An indexed copy may contain the user's annotations. A changed local
        # hash is not evidence that the remote source changed or needs repair.
        old_path = Path(old["path"])
        for candidate in local_candidates(search_dirs, None, None):
            if candidate == old_path:
                return {"status": "local_changed", "name": name, "path": str(candidate),
                        "bytes": size, "downloaded_bytes": 0,
                        "note": "来源版本未变，本地内容已变化（可能为批注或损坏）；保留本地文件，未重新下载，未改写原散列索引"}
    if dry_run:
        candidates = sum(1 for _ in local_candidates(search_dirs, size, None))
        return {"status": "would_check_content" if candidates else "would_download",
                "name": name, "bytes": size,
                "note": f"同大小候选 {candidates} 个；首次索引或来源变化需临时下载计算散列，不能仅凭文件名确认重复"}
    url = file.get("url")
    if not isinstance(url, str) or not url:
        raise FileRejected("学校未提供下载链接")
    client.validate_url(url, download=True)
    course.directory.mkdir(parents=True, exist_ok=True)
    staged, written, checksum, transferred, attempts = download_complete(client, course.directory, url, size, limit)
    try:
        matching = same_name_file(search_dirs, name)
        if matching:
            return name_preserved(file, name, matching, transferred)
        duplicate = find_duplicate(search_dirs, checksum, written, None)
        if not duplicate and old and checksum == old["sha256"]:
            # A changed source timestamp can be metadata-only. Once the newly
            # fetched source matches the original source hash, keep annotations
            # instead of publishing a fresh unannotated copy.
            for candidate in local_candidates(search_dirs, None, None):
                if candidate == Path(old["path"]):
                    store.save_file(course.id, fid, version(file), written, checksum, candidate)
                    return {"status": "local_changed", "name": name, "path": str(candidate),
                            "bytes": written, "downloaded_bytes": transferred, "download_attempts": attempts,
                            "version_unconfirmed": not version_known,
                            "note": "比对确认来源内容未变，仅版本标记变化；保留本地变化，未新增副本"}
        try:
            target = duplicate or publish_without_overwrite(staged, course.directory, name)
        except LocalNamePreserved as preserved:
            return name_preserved(file, name, preserved.path, transferred)
        store.save_file(course.id, fid, version(file), written, checksum, target)
        return {"status": "duplicate" if duplicate else "downloaded", "name": name,
                "path": str(target), "bytes": written, "downloaded_bytes": transferred, "download_attempts": attempts,
                "version_unconfirmed": not version_known}
    finally:
        # Only this run's temporary file is removed. Existing user files are untouched.
        staged.unlink(missing_ok=True)


def canonical_due(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ConnectionFailure("作业截止时间格式无效，未更新该课基线") from None
    if parsed.tzinfo is None:
        raise ConnectionFailure("作业截止时间缺少时区，未擅自推断")
    return parsed.astimezone(timezone.utc).isoformat()


def normalize_assignment(raw, cid, base):
    aid = identifier(raw["id"])
    submission = raw.get("submission")
    status = "unknown"
    if isinstance(submission, dict):
        if submission.get("excused") is True:
            status = "excused"
        elif submission.get("workflow_state") in {"submitted", "pending_review", "graded", "unsubmitted"}:
            status = submission["workflow_state"]
    types = raw.get("submission_types")
    if types == ["none"] and status in {"unknown", "unsubmitted"}:
        status = "no_online_submission"
    locked = raw.get("locked_for_user") is True
    lock_info = raw.get("lock_info")
    lock_info = lock_info if isinstance(lock_info, dict) else {}
    module = lock_info.get("context_module")
    module = module if isinstance(module, dict) else {}
    # The module date can be later than the assignment's own opening date.
    # These are availability dates, never substitutes for due_at.
    openings = []
    for value in (raw.get("unlock_at"), lock_info.get("unlock_at"), module.get("unlock_at")):
        if value:
            try:
                openings.append(canonical_due(value))
            except ConnectionFailure:
                pass  # Optional availability metadata must not hide the deadline.
    return {"id": aid, "title": clean(raw.get("name") or "未命名作业"),
            "due_at": canonical_due(raw.get("due_at")),
            "url": f"{base}/courses/{cid}/assignments/{aid}", "submission": status,
            "locked_for_user": locked, "unlock_at": max(openings) if openings else None}


def check_assignments(client, store, config, course, *, dry_run):
    raw = client.assignments(course.id)  # Complete all pages before touching the baseline.
    # A locked module can expose future assignment metadata to this student.
    # Keep its title/deadline without requesting or trying to unlock its content.
    items = [normalize_assignment(item, course.id, config.base_url) for item in raw
             if item.get("published") is not False]
    if len({a["id"] for a in items}) != len(items):
        raise ConnectionFailure("作业清单出现重复 ID，未更新基线")
    previous = store.assignments(course.id)
    course_state = store.course(course.id)
    first = not course_state or not course_state["baseline_at"]
    alerts = []
    if first:
        alerts.append(("baseline", {"count": len(items), "message": "首次建立基线；当前作业列于结果中，不逐条误报为新增"}))
    else:
        for item in items:
            old = previous.get(item["id"])
            if old is None:
                alerts.append(("new_assignment", item))
            elif old["due_at"] != item["due_at"]:
                alerts.append(("deadline_changed", {**item, "old_due_at": old["due_at"]}))
    if not dry_run:
        store.update_assignments(course.id, items, alerts, first=first)
    counts = {"received": len(raw), "listed": len(items),
              "locked": sum(item["locked_for_user"] for item in items),
              "unpublished": len(raw) - len(items),
              "pages": getattr(client, "page_counts", {}).get(f"/api/v1/courses/{course.id}/assignments")}
    return {"status": "preview" if dry_run else "checked", "first_run": first, "counts": counts,
            "assignments": items, "alerts": [{"kind": kind, **body} for kind, body in alerts]}


def run_sync(client, store, config, *, dry_run=False, assignments_only=False, compare_existing=False):
    results = []
    for course in config.courses:
        result = {"course": course.name, "course_id": course.id, "files": [], "errors": []}
        try:
            result["assignments"] = check_assignments(client, store, config, course, dry_run=dry_run)
        except LoginRequired:
            raise
        except (ConnectionFailure, OSError, ValueError, KeyError) as exc:
            result["errors"].append("作业检查失败：" + safe_error(exc))
        if not assignments_only:
            try:
                files = client.files(course.id)
                failed = False
                seen = set()
                for file in files:
                    fid = identifier(file["id"])
                    if fid in seen:
                        continue
                    seen.add(fid)
                    name = clean(file.get("display_name") or file.get("filename") or fid)
                    reason = eligible(file, course, config)
                    if reason:
                        result["files"].append({"status": "skipped", "name": name, "reason": reason})
                        continue
                    try:
                        synced = sync_file(client, store, config, course, file, dry_run=dry_run,
                                           compare_existing=compare_existing)
                        result["files"].append(synced)
                    except LoginRequired:
                        raise
                    except (ConnectionFailure, FileRejected, OSError, ValueError) as exc:
                        failed = True
                        result["files"].append({"status": "failed", "name": name, "reason": safe_error(exc)})
                if not dry_run and not failed:
                    store.files_checked(course.id)
            except LoginRequired:
                raise
            except (ConnectionFailure, FileRejected, OSError, ValueError, KeyError) as exc:
                result["errors"].append("文件检查失败：" + safe_error(exc))
        results.append(result)
    return results


def safe_error(error):
    if isinstance(error, (ConnectionFailure, FileRejected)):
        return clean(error)
    # Raw library exceptions can include signed URLs, server payloads or auth headers.
    return f"{type(error).__name__}，未记录原始响应；本次未标记成功"
