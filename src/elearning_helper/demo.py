"""Deterministic demo entirely inside an automatically cleaned temporary directory."""
import io
import tempfile
from email.message import Message
from pathlib import Path

from .config import Config, Course
from .state import Store
from .sync import run_sync


class DemoResponse(io.BytesIO):
    def __init__(self, body, content_type="application/pdf", content_length=True):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if content_length:
            self.headers["Content-Length"] = str(len(body))


class DemoClient:
    def __init__(self):
        self.download_count = 0
        self.bodies = {
            "https://elearning.fudan.edu.cn/demo/1": b"%PDF-1.4\nDemo lecture one\n%%EOF\n",
            "https://elearning.fudan.edu.cn/demo/2": b"%PDF-1.4\nDemo lecture two\n%%EOF\n",
            "https://elearning.fudan.edu.cn/demo/5": b"PK\x03\x04offline archive fixture; never extracted",
            "https://elearning.fudan.edu.cn/demo/6": b"raise RuntimeError('download only; never execute')\n",
            "https://elearning.fudan.edu.cn/demo/7": b"<!doctype html><html><h1>Offline course page</h1></html>",
        }
        self.file_rows = [
            {"id": 1, "display_name": "第一讲.pdf", "size": len(self.bodies["https://elearning.fudan.edu.cn/demo/1"]),
             "url": "https://elearning.fudan.edu.cn/demo/1", "content-type": "application/pdf", "modified_at": "v1"},
            {"id": 2, "display_name": "第二讲.pdf", "size": len(self.bodies["https://elearning.fudan.edu.cn/demo/2"]),
             "url": "https://elearning.fudan.edu.cn/demo/2", "content-type": "application/pdf", "modified_at": "v1"},
            {"id": 3, "display_name": "边界文件.bin", "size": 50_000_000},
            {"id": 4, "display_name": "尚未开放的文件.zip", "size": 100, "locked_for_user": True},
        ]
        for fid, name in [(5, "示例压缩包.zip"), (6, "示例代码.py"), (7, "示例网页.html")]:
            url = f"https://elearning.fudan.edu.cn/demo/{fid}"
            self.file_rows.append({"id": fid, "display_name": name, "size": len(self.bodies[url]),
                                   "url": url, "modified_at": "v1"})
        self.assignment_rows = [{"id": 11, "name": "示例作业一", "due_at": "2026-10-10T15:59:00Z",
                                 "submission": {"workflow_state": "unsubmitted"}}]

    def validate_url(self, url, *, download=False):
        if url not in self.bodies:
            raise ValueError("未知演示文件")

    def open(self, url, *, download=False):
        self.download_count += 1
        return DemoResponse(self.bodies[url])

    def files(self, cid):
        return self.file_rows

    def folders(self, cid):
        return []

    def assignments(self, cid):
        return self.assignment_rows


def demo_config(root):
    root = Path(root).resolve()
    return Config("https://elearning.fudan.edu.cn", root / "courses", root / "state",
                  50_000_000, "FUDAN_ELEARNING_TOKEN", "Asia/Shanghai",
                  ("canvas-production.s3.fudan.edu.cn",), (".pdf", ".ppt", ".pptx"),
                  ("第.+讲", "chapter"), ("教材|textbook|作业|实验|数据集",),
                  (Course("114614", "演示课程（模拟数据）", root / "courses" / "算法" / "课件", ()),))


def run_demo():
    with tempfile.TemporaryDirectory(prefix="elearning-helper-demo-") as folder:
        config = demo_config(folder)
        client = DemoClient()
        directory = config.courses[0].directory
        directory.mkdir(parents=True)
        (directory / "已有第一讲.pdf").write_bytes(client.bodies["https://elearning.fudan.edu.cn/demo/1"])
        store = Store(config.state_dir / "index.sqlite3")
        try:
            first = run_sync(client, store, config)
            client.assignment_rows[0] = {**client.assignment_rows[0], "due_at": "2026-10-12T15:59:00Z"}
            client.assignment_rows.append({"id": 12, "name": "示例新作业", "due_at": None})
            second = run_sync(client, store, config)
            third = run_sync(client, store, config)
            return {"mode": "demo", "school_contacted": False, "real_courses_changed": False,
                    "first_sync": first, "second_sync": second, "third_sync": third,
                    "download_requests": client.download_count,
                    "note": "演示用临时文件和索引已自动清理；未访问学校、凭据或真实课程目录"}
        finally:
            store.close()
