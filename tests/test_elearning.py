import contextlib
import copy
import io
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from src.elearning_helper.__main__ import main
from src.elearning_helper.api import CanvasClient, ConnectionFailure, LoginRequired
from src.elearning_helper.config import HARD_LIMIT, load_config
from src.elearning_helper.demo import DemoClient, DemoResponse, demo_config, run_demo
from src.elearning_helper.state import Store, run_lock
from src.elearning_helper.sync import (FileRejected, canonical_due, check_assignments, eligible,
                                   normalize_assignment, run_sync, sync_file,
                                   reject_login_document)

PROJECT = Path(__file__).resolve().parent.parent


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = demo_config(self.root)
        self.course = self.config.courses[0]
        self.store = Store(self.config.state_dir / "index.sqlite3")
        self.addCleanup(self.store.close)
        self.client = DemoClient()
        self.file = self.client.file_rows[0]

    def download(self, file=None, **kwargs):
        return sync_file(self.client, self.store, self.config, self.course, file or self.file,
                         dry_run=False, **kwargs)

    def test_new_then_repeated_run_needs_no_download(self):
        first = self.download()
        self.assertEqual(first["status"], "downloaded")
        second = self.download()
        self.assertEqual(second["status"], "name_preserved")
        self.assertEqual(self.client.download_count, 1)
        self.assertEqual(len(list(self.course.directory.iterdir())), 1)

    def test_existing_renamed_content_is_reused(self):
        self.course.directory.mkdir(parents=True)
        old = self.course.directory / "第一讲 (1).pdf"
        old.write_bytes(self.client.bodies[self.file["url"]])
        result = self.download()
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["path"], str(old))
        self.assertEqual(list(self.course.directory.iterdir()), [old])

    def test_changed_same_name_keeps_existing_bytes(self):
        first = self.download()
        original = Path(first["path"]).read_bytes()
        new_body = b"%PDF-1.4\nchanged lecture\n%%EOF\n"
        self.client.bodies[self.file["url"]] = new_body
        changed = {**self.file, "size": len(new_body), "modified_at": "v2"}
        second = self.download(changed)
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(second["status"], "name_preserved")
        self.assertEqual(Path(first["path"]).read_bytes(), original)
        self.assertEqual(Path(second["path"]).read_bytes(), original)
        self.assertEqual(self.client.download_count, 1)

    def test_local_modified_file_is_preserved(self):
        first = self.download()
        path = Path(first["path"])
        path.write_bytes(b"local user edits")
        second = self.download()
        self.assertEqual(path.read_bytes(), b"local user edits")
        self.assertEqual(str(path), second["path"])
        self.assertEqual(second["status"], "name_preserved")
        self.assertEqual(self.client.download_count, 1)

    def test_failed_index_commit_can_recover_by_content(self):
        with patch.object(self.store, "save_file", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.download()
        self.assertIsNone(self.store.file(self.course.id, "1"))
        self.assertEqual(len(list(self.course.directory.glob("*.pdf"))), 1)
        self.assertEqual(self.download()["status"], "name_preserved")
        recovered = self.download(compare_existing=True)
        self.assertEqual(recovered["status"], "name_preserved")
        self.assertIsNone(self.store.file(self.course.id, "1"))
        self.assertEqual(self.client.download_count, 1)

    def test_interrupted_download_never_publishes_and_retry_succeeds(self):
        full = self.client.bodies[self.file["url"]]
        with patch("src.elearning_helper.sync.time.sleep"), patch.object(self.client, "open",
                side_effect=lambda *args, **kwargs: DemoResponse(full[:-10], content_length=False)):
            with self.assertRaises(FileRejected):
                self.download()
        self.assertEqual(list(self.course.directory.iterdir()), [])
        self.assertIsNone(self.store.file(self.course.id, "1"))
        self.assertEqual(self.download()["status"], "downloaded")

    def test_stream_limit_without_length_stops_before_publishing(self):
        self.config = replace(self.config, max_bytes=40)
        response = DemoResponse(b"x" * 100, content_length=False)
        with patch.object(self.client, "open", return_value=response):
            with self.assertRaisesRegex(FileRejected, "超过"):
                self.download()
        self.assertFalse(any(self.course.directory.iterdir()))

    def test_header_size_limit_rejects_without_reading(self):
        response = DemoResponse(b"x" * 100)
        self.config = replace(self.config, max_bytes=40)
        with patch.object(response, "read", side_effect=AssertionError("body must not be read")):
            with patch.object(self.client, "open", return_value=response):
                with self.assertRaisesRegex(FileRejected, "上限"):
                    self.download()

    def test_exact_stream_limit_is_rejected_before_download(self):
        self.config = replace(self.config, max_bytes=self.file["size"])
        with self.assertRaises(FileRejected):
            self.download()
        self.assertEqual(self.client.download_count, 0)

    def test_html_disguised_as_pdf_is_rejected(self):
        html = b'<html><title>Canvas Login</title><form><input type="password"></form></html>'
        self.file = {**self.file, "size": len(html)}
        with patch.object(self.client, "open", return_value=DemoResponse(html, "text/html")):
            with self.assertRaises(LoginRequired):
                self.download()
        self.assertFalse(any(self.course.directory.iterdir()))

    def test_bytes_are_not_rejected_based_on_pdf_filename(self):
        body = b"arbitrary complete file bytes"
        self.file = {**self.file, "size": len(body)}
        with patch.object(self.client, "open", return_value=DemoResponse(body, "application/octet-stream")):
            result = self.download()
        self.assertEqual(Path(result["path"]).read_bytes(), body)

    def test_path_traversal_name_cannot_escape_course(self):
        self.file = {**self.file, "display_name": "../../第一讲.pdf"}
        result = self.download()
        self.assertEqual(Path(result["path"]).parent, self.course.directory)

    def test_symlink_target_rejected(self):
        real = self.root / "elsewhere"
        real.mkdir()
        self.course.directory.parent.mkdir(parents=True)
        self.course.directory.symlink_to(real, target_is_directory=True)
        with self.assertRaises(FileRejected):
            self.download()
        self.assertEqual(list(real.iterdir()), [])

    def test_symlink_filename_never_overwritten(self):
        self.course.directory.mkdir(parents=True)
        elsewhere = self.root / "preserve.pdf"
        elsewhere.write_bytes(b"keep")
        (self.course.directory / self.file["display_name"]).symlink_to(elsewhere)
        with self.assertRaisesRegex(FileRejected, "不是普通文件"):
            self.download()
        self.assertEqual(elsewhere.read_bytes(), b"keep")
        self.assertEqual(self.client.download_count, 0)

    def test_dry_run_does_not_download_or_mutate_index(self):
        result = run_sync(self.client, self.store, self.config, dry_run=True)
        self.assertEqual(result[0]["files"][0]["status"], "would_download")
        self.assertEqual(self.client.download_count, 0)
        self.assertFalse(self.course.directory.exists())
        self.assertIsNone(self.store.course(self.course.id))
        self.assertEqual(self.store.alerts(), [])

    def test_readonly_store_missing_path_creates_no_directory(self):
        directory = self.root / "absent"
        store = Store(directory / "index.sqlite3", readonly=True)
        try:
            self.assertEqual(store.alerts(), [])
        finally:
            store.close()
        self.assertFalse(directory.exists())

    def test_decimal_50mb_limit_and_filters(self):
        for size, accepted in ((49_999_999, True), (50_000_000, False), (50_000_001, False), (50 * 1024 * 1024, False)):
            reason = eligible({**self.file, "size": size}, self.course, self.config, {})
            self.assertEqual(reason is None, accepted)
        for name in ("教材.pdf", "作业第一讲.pdf", "第一讲.mp4", "第一讲.zip", "dataset.pdf", "other.pdf"):
            self.assertIsNone(eligible({**self.file, "display_name": name}, self.course, self.config, {}))

    def test_allowlisted_course_folder(self):
        course = replace(self.course, folders=("课程ppt",))
        file = {**self.file, "display_name": "Embedding.pdf", "folder_id": 10}
        self.assertIsNone(eligible(file, course, self.config, {"10": "course files/课程ppt"}))

    def test_ordinary_html_is_allowed(self):
        html = self.root / "lesson.html"
        html.write_bytes(b"<!doctype html><html><h1>Course content</h1></html>")
        reject_login_document(html)

    def test_baseline_then_new_and_deadline_change_only_once(self):
        first = check_assignments(self.client, self.store, self.config, self.course, dry_run=False)
        self.assertEqual([x["kind"] for x in first["alerts"]], ["baseline"])
        self.client.assignment_rows[0]["due_at"] = "2026-10-12T15:59:00Z"
        self.client.assignment_rows.append({"id": 12, "name": "New", "due_at": None})
        second = check_assignments(self.client, self.store, self.config, self.course, dry_run=False)
        self.assertEqual([x["kind"] for x in second["alerts"]], ["deadline_changed", "new_assignment"])
        third = check_assignments(self.client, self.store, self.config, self.course, dry_run=False)
        self.assertEqual(third["alerts"], [])
        self.assertEqual(len(self.store.alerts()), 3)

    def test_due_date_clear_and_revert_each_remind(self):
        check_assignments(self.client, self.store, self.config, self.course, dry_run=False)
        for new_due in (None, "2026-10-10T15:59:00Z", None):
            self.client.assignment_rows[0]["due_at"] = new_due
            result = check_assignments(self.client, self.store, self.config, self.course, dry_run=False)
            self.assertEqual(result["alerts"][0]["kind"], "deadline_changed")

    def test_timezone_equivalent_due_date_does_not_alert(self):
        check_assignments(self.client, self.store, self.config, self.course, dry_run=False)
        self.client.assignment_rows[0]["due_at"] = "2026-10-10T23:59:00+08:00"
        result = check_assignments(self.client, self.store, self.config, self.course, dry_run=False)
        self.assertEqual(result["alerts"], [])

    def test_unconfirmed_submission_is_not_claimed_submitted(self):
        row = normalize_assignment({"id": 3, "name": "x", "has_submitted_submissions": True}, "1", "https://example.test")
        self.assertEqual(row["submission"], "unknown")
        with self.assertRaises(ConnectionFailure):
            canonical_due("2026-10-10T12:00:00")

    def test_assignment_error_preserves_baseline(self):
        check_assignments(self.client, self.store, self.config, self.course, dry_run=False)
        old = dict(self.store.course(self.course.id))
        self.client.assignment_rows.append({"id": 12, "due_at": "not a date"})
        with self.assertRaises(ConnectionFailure):
            check_assignments(self.client, self.store, self.config, self.course, dry_run=False)
        self.assertEqual(dict(self.store.course(self.course.id)), old)
        self.assertNotIn("12", self.store.assignments(self.course.id))

    def test_login_failure_does_not_initialize_baseline(self):
        with patch.object(self.client, "assignments", side_effect=LoginRequired("expired")):
            with self.assertRaises(LoginRequired):
                run_sync(self.client, self.store, self.config)
        self.assertIsNone(self.store.course(self.course.id))

    def test_alert_ack_is_local_and_retains_history(self):
        check_assignments(self.client, self.store, self.config, self.course, dry_run=False)
        self.store.acknowledge([1])
        self.assertEqual(self.store.alerts(), [])
        self.assertEqual(len(self.store.alerts(include_read=True)), 1)

    def test_lock_prevents_simultaneous_sync(self):
        with run_lock(self.config.state_dir):
            with self.assertRaises(RuntimeError):
                with run_lock(self.config.state_dir):
                    pass


class CliTests(unittest.TestCase):
    def test_demo_three_runs(self):
        result = run_demo()
        self.assertEqual(result["download_requests"], 5)
        self.assertEqual(result["third_sync"][0]["assignments"]["alerts"], [])
        self.assertFalse(result["school_contacted"])

    def test_bundled_config_has_courses_and_enforces_size_limit(self):
        config = load_config(PROJECT / "src/elearning_helper/config.json")
        self.assertTrue(config.courses)
        self.assertEqual(config.max_bytes, 50_000_000)

    def test_missing_auth_exits_3_before_creating_state(self):
        with tempfile.TemporaryDirectory() as folder:
            config = json.loads((PROJECT / "src/elearning_helper/config.json").read_text())
            config["root"] = str((Path(folder) / "courses").resolve())
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(config))
            with patch.dict(os.environ, {config["auth_env"]: ""}):
                with contextlib.redirect_stderr(io.StringIO()):
                    code = main(["--config", str(path), "sync", "--dry-run"])
            self.assertEqual(code, 3)
            self.assertFalse((Path(folder) / ".state").exists())

    def test_doctor_never_reads_runtime_credentials(self):
        original_get = os.environ.get
        def guard(key, default=None):
            if key == "FUDAN_ELEARNING_TOKEN":
                raise AssertionError("must not read secrets")
            return original_get(key, default)
        with patch.object(os.environ, "get", side_effect=guard):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["doctor"]), 0)

    def test_reject_path_escape_and_limit_increase(self):
        with tempfile.TemporaryDirectory() as folder:
            original = json.loads((PROJECT / "src/elearning_helper/config.json").read_text())
            for change in ("limit", "path"):
                data = copy.deepcopy(original)
                if change == "limit":
                    data["max_bytes"] = HARD_LIMIT + 1
                else:
                    data["courses"][0]["directory"] = "../escape"
                path = Path(folder) / "config.json"
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    load_config(path)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requests = []
        cls.mode = "normal"
        cls.pdf = b"%PDF-1.4\nHTTP fixture\n%%EOF\n"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                cls.requests.append((self.path, self.headers.get("Authorization")))
                if self.path == "/unauthorized":
                    self.send_response(401)
                    self.end_headers()
                    return
                if self.path == "/forbidden":
                    self.send_response(403)
                    self.end_headers()
                    return
                if self.path == "/external":
                    self.send_response(302)
                    self.send_header("Location", "https://unapproved.invalid/collect")
                    self.end_headers()
                    return
                if self.path == "/storage-redirect":
                    self.send_response(302)
                    self.send_header("Location", cls.storage_base + "/pdf")
                    self.end_headers()
                    return
                if self.path == "/pdf":
                    body, content_type = cls.pdf, "application/pdf"
                elif self.path.startswith("/api/v1/courses/114614/files"):
                    body = json.dumps([{"id": 1, "display_name": "第一讲.pdf", "size": len(cls.pdf),
                                        "content-type": "application/pdf", "modified_at": "2026-10-04",
                                        "url": cls.base + "/pdf"}]).encode()
                    content_type = "application/json"
                elif self.path.startswith("/api/v1/courses/114614/folders"):
                    body, content_type = b"[]", "application/json"
                elif self.path.startswith("/api/v1/courses/114614/assignments"):
                    if cls.mode == "html":
                        body, content_type = b"<html>sign in</html>", "text/html"
                    elif "page=2" in self.path:
                        body = json.dumps([{"id": 2, "name": "second", "due_at": None}]).encode()
                        content_type = "application/json"
                    else:
                        body = json.dumps([{"id": 1, "name": "first", "due_at": None}]).encode()
                        content_type = "application/json"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                if "/assignments?" in self.path and "page=2" not in self.path and cls.mode == "normal":
                    self.send_header("Link", f'<{cls.base}/api/v1/courses/114614/assignments?page=2>; rel="next"')
                self.end_headers()
                self.wfile.write(body)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.storage = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.storage_base = f"http://127.0.0.1:{cls.storage.server_port}"
        cls.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (cls.server, cls.storage)]
        for thread in cls.threads:
            thread.start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.storage):
            server.shutdown()
            server.server_close()
        for thread in cls.threads:
            thread.join()

    def setUp(self):
        type(self).requests.clear()
        type(self).mode = "normal"
        # Deliberately fake fixture string; not a real token or persisted credential.
        self.client = CanvasClient(self.base, "TEST_ONLY_NOT_A_CREDENTIAL", ("127.0.0.1",), allow_loopback=True)

    def test_real_http_pagination_download_and_repeat(self):
        with tempfile.TemporaryDirectory() as folder:
            config = replace(demo_config(folder), base_url=self.base)
            store = Store(config.state_dir / "index.sqlite3")
            try:
                result = run_sync(self.client, store, config)
                self.assertEqual(len(result[0]["assignments"]["assignments"]), 2)
                self.assertEqual(result[0]["files"][0]["status"], "downloaded")
                repeat = run_sync(self.client, store, config)
                self.assertEqual(repeat[0]["files"][0]["status"], "name_preserved")
                self.assertEqual(repeat[0]["assignments"]["alerts"], [])
                self.assertEqual(sum(path == "/pdf" for path, auth in self.requests), 1)
                self.assertTrue(any("override_assignment_dates=true" in path for path, auth in self.requests))
                self.assertNotIn(b"TEST_ONLY_NOT_A_CREDENTIAL", (config.state_dir / "index.sqlite3").read_bytes())
            finally:
                store.close()

    def test_storage_redirect_strips_authorization(self):
        with self.client.open(self.base + "/storage-redirect", download=True) as response:
            self.assertEqual(response.read(), self.pdf)
        self.assertEqual(self.requests[-1], ("/pdf", None))

    def test_unapproved_redirect_blocked_without_request(self):
        with self.assertRaises(ConnectionFailure):
            self.client.open(self.base + "/external", download=True)
        self.assertEqual(len(self.requests), 1)

    def test_401_distinct_from_403(self):
        with self.assertRaises(LoginRequired):
            self.client.open(self.base + "/unauthorized")
        with self.assertRaises(ConnectionFailure) as caught:
            self.client.open(self.base + "/forbidden")
        self.assertNotIsInstance(caught.exception, LoginRequired)

    def test_html_api_response_not_treated_as_empty(self):
        type(self).mode = "html"
        with self.assertRaises(LoginRequired):
            self.client.assignments("114614")

    def test_metadata_other_origin_and_non_https_rejected(self):
        with self.assertRaises(ConnectionFailure):
            self.client.validate_url(self.storage_base + "/anything")
        real = CanvasClient("https://elearning.fudan.edu.cn", "TEST_ONLY_NOT_A_CREDENTIAL")
        with self.assertRaises(ConnectionFailure):
            real.validate_url("http://elearning.fudan.edu.cn/api/v1/courses")


if __name__ == "__main__":
    unittest.main()
