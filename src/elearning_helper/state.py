import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS courses(id TEXT PRIMARY KEY, baseline_at TEXT, assignments_checked_at TEXT, files_checked_at TEXT);
CREATE TABLE IF NOT EXISTS files(course_id TEXT, file_id TEXT, version TEXT, size INTEGER,
 sha256 TEXT, path TEXT, PRIMARY KEY(course_id,file_id));
CREATE TABLE IF NOT EXISTS assignments(course_id TEXT, assignment_id TEXT, title TEXT,
 due_at TEXT, url TEXT, submission TEXT, PRIMARY KEY(course_id,assignment_id));
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, course_id TEXT,
 kind TEXT, body TEXT, created_at TEXT, acknowledged INTEGER NOT NULL DEFAULT 0);
"""


class Store:
    def __init__(self, path, *, readonly=False):
        self.readonly = readonly
        path = Path(path)
        if readonly and path.exists():
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        elif readonly:
            self.db = sqlite3.connect(":memory:")
            self.db.executescript(SCHEMA)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, timeout=10)
            self.db.executescript(SCHEMA)
            self.db.commit()
        self.db.row_factory = sqlite3.Row

    def close(self):
        self.db.close()

    def file(self, cid, fid):
        return self.db.execute("SELECT * FROM files WHERE course_id=? AND file_id=?", (cid, fid)).fetchone()

    def save_file(self, cid, fid, version, size, digest, path):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?)",
                            (cid, fid, version, size, digest, str(path)))

    def course(self, cid):
        return self.db.execute("SELECT * FROM courses WHERE id=?", (cid,)).fetchone()

    def assignments(self, cid):
        return {row["assignment_id"]: dict(row) for row in self.db.execute(
            "SELECT * FROM assignments WHERE course_id=?", (cid,))}

    def update_assignments(self, cid, assignments, alerts, *, first):
        with self.db:
            timestamp = now()
            self.db.execute("INSERT OR IGNORE INTO courses(id) VALUES(?)", (cid,))
            for item in assignments:
                self.db.execute("INSERT OR REPLACE INTO assignments VALUES(?,?,?,?,?,?)",
                                (cid, item["id"], item["title"], item["due_at"], item["url"], item["submission"]))
            for kind, body in alerts:
                self.db.execute("INSERT INTO alerts(course_id,kind,body,created_at) VALUES(?,?,?,?)",
                                (cid, kind, json.dumps(body, ensure_ascii=False), timestamp))
            self.db.execute("UPDATE courses SET assignments_checked_at=? WHERE id=?", (timestamp, cid))
            if first:
                self.db.execute("UPDATE courses SET baseline_at=? WHERE id=?", (timestamp, cid))

    def files_checked(self, cid):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO courses(id) VALUES(?)", (cid,))
            self.db.execute("UPDATE courses SET files_checked_at=? WHERE id=?", (now(), cid))

    def alerts(self, *, include_read=False):
        query = "SELECT * FROM alerts" + ("" if include_read else " WHERE acknowledged=0") + " ORDER BY id"
        return [dict(row) for row in self.db.execute(query)]

    def acknowledge(self, ids):
        with self.db:
            for value in ids:
                self.db.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (value,))


@contextlib.contextmanager
def run_lock(state_dir):
    """Process lock, automatically released by the OS after a crash (macOS/Linux)."""
    import fcntl
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "run.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("已有同步进程运行，本次未开始") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
