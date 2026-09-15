"""Disk-backed stage queues. Planning never fills an unbounded Python queue.

Each process owns an exclusive run lock. Interrupted jobs remain in the audit
log; the next plan reuses verified artifacts and requeues unfinished stages.
"""

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .artifacts import atomic_write_json, cached_file_sha256, file_sha256, internal_path, migrate_auxiliary


class PipelineState:
    def __init__(self, path, run_id=None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.condition = threading.Condition()
        self.run_id = run_id or uuid.uuid4().hex
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS pipeline_jobs (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL,
                course_id TEXT, sub_id TEXT, payload TEXT NOT NULL,
                status TEXT NOT NULL, error TEXT, updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS pipeline_queue ON pipeline_jobs(run_id, stage, status, id);
        """)
        with self.connection:
            self.connection.execute(
                "UPDATE pipeline_jobs SET status='interrupted' WHERE status IN ('queued','running')"
            )

    def queue(self, stage):
        return StageQueue(self, stage)

    def close(self):
        with self.condition:
            self.connection.close()


class StageQueue:
    def __init__(self, state, stage):
        self.state, self.stage = state, stage
        self.current = threading.local()

    def put(self, task):
        payload = json.dumps(task, ensure_ascii=False, default=lambda p: str(p) if isinstance(p, Path) else p)
        with self.state.condition, self.state.connection:
            self.state.connection.execute(
                "INSERT INTO pipeline_jobs(run_id,stage,course_id,sub_id,payload,status,updated_at) "
                "VALUES(?,?,?,?,?,'queued',?)",
                (self.state.run_id, self.stage, task.get("course_id") if task else None,
                 task.get("sub_id") if task else None, payload, time.time()),
            )
            self.state.condition.notify_all()

    def get(self):
        with self.state.condition:
            while True:
                row = self.state.connection.execute(
                    "SELECT id,payload FROM pipeline_jobs WHERE run_id=? AND stage=? AND status='queued' "
                    "ORDER BY id LIMIT 1", (self.state.run_id, self.stage),
                ).fetchone()
                if row:
                    with self.state.connection:
                        self.state.connection.execute(
                            "UPDATE pipeline_jobs SET status='running',updated_at=? WHERE id=?",
                            (time.time(), row["id"]),
                        )
                    self.current.id = row["id"]
                    task = json.loads(row["payload"])
                    if task:
                        for key, value in task.items():
                            if key.endswith("_path") and value:
                                task[key] = Path(value)
                    return task
                self.state.condition.wait(timeout=0.5)

    def mark(self, status, error=None):
        with self.state.condition, self.state.connection:
            self.state.connection.execute(
                "UPDATE pipeline_jobs SET status=?,error=?,updated_at=? WHERE id=?",
                (status, error, time.time(), self.current.id),
            )

    def task_done(self):
        with self.state.condition, self.state.connection:
            self.state.connection.execute(
                "UPDATE pipeline_jobs SET status='done',updated_at=? WHERE id=? AND status='running'",
                (time.time(), self.current.id),
            )


def mark_stage(queue, status, error=None):
    if hasattr(queue, "mark"):
        queue.mark(status, error)


def artifact_metadata(path, *, source=None, settings_fingerprint=None, **metadata):
    path = Path(path)
    source = Path(source) if source else None
    atomic_write_json(migrate_auxiliary(path.with_suffix(path.suffix + ".icourse.json")), {
        "schema": 1, "sha256": file_sha256(path),
        "source": str(source.resolve()) if source else None,
        "source_sha256": file_sha256(source) if source else None,
        "settings_fingerprint": settings_fingerprint, **metadata,
    })


def valid_artifact(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return False
    legacy_marker = path.with_suffix(path.suffix + ".icourse.json")
    marker = internal_path(legacy_marker)
    if not marker.exists() and legacy_marker.exists():
        # Validation also works on read-only media; migration is a separate write.
        marker = legacy_marker
    if not marker.exists():
        # Existing ID-bearing files are legacy imports, never silently deleted.
        return True
    try:
        metadata = json.loads(marker.read_text(encoding="utf-8"))
        if metadata.get("status") == "needs_review":
            return False
        if metadata["sha256"] != cached_file_sha256(path):
            return False
        source = metadata.get("source")
        if source and (not Path(source).is_file() or cached_file_sha256(source) != metadata["source_sha256"]):
            return False
        if path.suffix == ".txt":
            from .asr import ASRSettings
            if metadata.get("settings_fingerprint") != ASRSettings.from_env().fingerprint:
                return False
        if path.suffix == ".md":
            # Completed notes belong to the user. New LLM defaults only apply
            # to new work; explicit --overwrite requests regeneration.
            if source and not valid_artifact(source):
                return False
        return True
    except (ValueError, KeyError, OSError):
        return False
