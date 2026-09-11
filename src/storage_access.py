"""Check storage using real I/O in the same process that will run the task."""

import errno
import os
import sys
import tempfile
from pathlib import Path


class StorageAccessError(RuntimeError):
    pass


def storage_error(path: Path, label: str, error: OSError) -> StorageAccessError:
    message = f"无法访问{label}：{path}"
    if error.errno in (errno.EPERM, errno.EACCES):
        message += "\n当前运行进程没有访问权限。"
        if sys.platform == "darwin":
            message += (
                "请在 App 中点击保存位置旁的“选择…”，重新选择此文件夹；"
                "若系统询问访问权限，点击 Allow。\n"
                "也可在 System Settings → Privacy & Security → Files & Folders 中，"
                "为实际启动的 iCourse 或 Terminal 开启 Documents Folder / Removable Volumes 等对应权限。"
                "修改后退出并重开 App。Finder 能访问不代表 App 已获授权。"
            )
        else:
            message += "请检查该目录对当前用户的读取和写入权限。"
    elif error.errno == errno.EROFS:
        message += "\n磁盘处于只读状态，请检查挂载状态或选择可写目录。"
    elif error.errno == errno.ENOSPC:
        message += "\n磁盘空间不足，请释放空间或选择其他保存位置。"
    else:
        message += f"\n{error.strerror or error}"
    return StorageAccessError(message)


def check_directory(path: Path, label: str, *, writable=False, create=False, missing_ok=False) -> None:
    """Do not trust exists()/os.access(): macOS privacy denial can occur at scandir."""
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        try:
            path.stat()
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        with os.scandir(path) as entries:
            next(entries, None)
        if writable:
            # Unique temporary file: never open or overwrite a user's artifact.
            with tempfile.TemporaryFile(dir=path, prefix=".icourse-access-") as stream:
                stream.write(b"\0")
                stream.flush()
    except OSError as exc:
        raise storage_error(path, label, exc) from exc


def check_pipeline_storage(video: Path, notes: Path, *, mode: str, list_only: bool) -> None:
    if list_only and mode != "summarize":
        return  # Online listing does not use either local output directory.
    check_directory(video, "课程保存位置", writable=not list_only,
                    create=not list_only and mode != "summarize", missing_ok=mode == "summarize")
    if mode != "download":
        check_directory(notes, "笔记保存位置", writable=not list_only,
                        create=not list_only, missing_ok=list_only)
