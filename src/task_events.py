"""Versioned, ordered desktop events. Never include request bodies or credentials."""

import contextvars
import json
import os
import re
import sys
import threading
import time
import uuid

_context = contextvars.ContextVar("icourse_task", default={})
_lock = threading.RLock()
_run_id = ""
_seq = 0
_started = 0.0
_stream = None
_secrets = ()


def enabled():
    return os.environ.get("ICOURSE_EVENTS") == "json"


def set_secrets(values):
    global _secrets
    _secrets = tuple(sorted({str(v) for v in values if v and len(str(v)) >= 4}, key=len, reverse=True))


def redact(text, *, export=False):
    text = str(text)
    for value in _secrets:
        text = text.replace(value, "[已隐藏]")
    text = re.sub(r"https?://[^\s<>\"']+", "[链接已隐藏]", text)
    text = re.sub(r"(?i)(bearer\s+)[\w.\-/+=]+", r"\1[已隐藏]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|password|uispsw|cookie|authorization|ticket|loginToken|access_token)\s*[\"']?\s*[:=]\s*)[^\r\n]+", r"\1[已隐藏]", text)
    text = re.sub(r"(?i)([\"'](?:messages|prompt|content)[\"']\s*:)\s*[^\r\n]+", r"\1 [正文已隐藏]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[已隐藏]", text)
    if export:
        text = text.replace(os.path.expanduser("~"), "~")
    return text


def _safe(value):
    if isinstance(value, str):
        return redact(value)[:4000]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return value


def begin_run(run_id=None, **data):
    global _run_id, _seq, _started, _stream
    with _lock:
        run_id = run_id or uuid.uuid4().hex
        if run_id == _run_id:
            return
        _run_id, _seq, _started, _stream = run_id, 0, time.monotonic(), sys.stdout
        _context.set({})
        set_secrets(v for k, v in os.environ.items() if any(s in k.upper() for s in ("PASSWORD", "API_KEY", "UISPSW", "TOKEN")))
        emit("run_started", **data)


def emit(kind, **data):
    global _seq
    if not enabled() or not _run_id:
        return
    with _lock:
        _seq += 1
        event = dict(event="icourse", version=1, run_id=_run_id, seq=_seq,
                     elapsed=max(0, time.monotonic() - _started), time=time.strftime("%H:%M:%S"), kind=kind)
        event.update(_safe(data))
        try:
            _stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            _stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass  # Display failures must not turn a saved artifact into a failed job.


def identity(task):
    return dict(course_id=str(task["course_id"]), sub_id=str(task["sub_id"]),
                course_title=task.get("course_title", str(task["course_id"])),
                title=task.get("sub_title", str(task["sub_id"])))


def bind_task(task, stage):
    _context.set({**identity(task), "stage": stage})


def progress(message="", **metrics):
    context = _context.get()
    if context:
        emit("progress", **context, message=message, metrics=metrics)
    elif message:
        emit("phase", message=message)


def stage(task, name, status, message="", **data):
    if status == "running":
        bind_task(task, name)
    emit("stage", **identity(task), stage=name, status=status, message=message, **data)


def plan_task(task, stages, **paths):
    emit("task", **identity(task), stages=stages,
         paths={key: str(value) for key, value in paths.items() if value is not None})
