import queue
import threading

import pytest

from src.artifacts import atomic_write_text
from src.pipeline_state import PipelineState, artifact_metadata, valid_artifact
from tools.icourse_video_downloader import downloader as d


def test_same_title_different_lectures_never_share_output(tmp_path):
    a = d._resolve_target_path(tmp_path, "同名课", "123456", ".mp4")
    b = d._resolve_target_path(tmp_path, "同名课", "123457", ".mp4")
    assert a != b
    a.write_bytes(b"first")
    assert d._resolve_target_path(tmp_path, "同名课", "123457", ".mp4") == b


def test_legacy_bare_title_video_is_found_without_guessing_online_id(tmp_path):
    media = tmp_path / "2026-09-06 第一次课.mp4"
    media.write_bytes(b"video")
    lectures = d._collect_local_lectures([tmp_path], set())
    assert len(lectures) == 1
    assert lectures[0]["sub_id"].startswith("local-")
    assert lectures[0]["sub_title"] == media.stem
    assert d._find_file_for_lecture([tmp_path], media.stem, "123456", ".mp4") is None
    renamed = tmp_path / "renamed.mp4"
    media.rename(renamed)
    assert d._collect_local_lectures([tmp_path], set())[0]["sub_id"] == lectures[0]["sub_id"]


def test_pending_marker_and_corrupt_artifact_not_reused(tmp_path):
    p = tmp_path / "note_123456.md"
    p.write_text("partial")
    d._begin_artifact(p)
    assert not valid_artifact(p)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"valid")
    artifact_metadata(video)
    assert valid_artifact(video)
    video.write_bytes(b"corrupt")
    assert not valid_artifact(video)


def test_disk_queue_fifo_paths_and_failed_state_survive_restart(tmp_path):
    path = tmp_path / "state.sqlite"
    state = PipelineState(path)
    stage = state.queue("tr")
    task = dict(course_id="123", sub_id="456", video_path=tmp_path / "课.mp4")
    for i in range(100):
        stage.put({**task, "sub_id": str(i)})
    stage.put(None)
    assert stage.get()["video_path"] == task["video_path"]
    stage.mark("failed", "test error")
    stage.task_done()
    assert state.connection.execute("SELECT status FROM pipeline_jobs ORDER BY id LIMIT 1").fetchone()[0] == "failed"
    state.close()
    state = PipelineState(path)
    rows = dict(state.connection.execute("SELECT status,count(*) FROM pipeline_jobs GROUP BY status").fetchall())
    assert rows == {"failed": 1, "interrupted": 100}
    state.close()


def test_queue_consumers_can_start_before_planning(tmp_path):
    state = PipelineState(tmp_path / "state.sqlite")
    stage = state.queue("dl")
    found = []

    def consume():
        found.append(stage.get())
        stage.task_done()

    worker = threading.Thread(target=consume)
    worker.start()
    stage.put({"course_id": "1", "sub_id": "2"})
    worker.join(timeout=2)
    assert not worker.is_alive() and found[0]["sub_id"] == "2"
    state.close()


def test_summary_read_failure_does_not_kill_consumer(tmp_path):
    q = queue.Queue()
    q.put(dict(course_id="1", sub_id="2", sub_title="课", target_transcript_path=tmp_path / "missing.txt"))
    q.put(None)
    counters = d._Counters()
    d._summarize_stage(q, None, 0, counters, set(), threading.Lock())
    assert counters.failed == 1 and q.unfinished_tasks == 0


def test_api_failure_is_not_reported_as_unpublished(monkeypatch):
    from src.icourse import ICourseClient
    client = ICourseClient(None)
    def fail(*_):
        raise RuntimeError("authentication expired")
    monkeypatch.setattr(client, "get_sub_info", fail)
    with pytest.raises(RuntimeError, match="authentication expired"):
        client.get_video_url("1", "2")


def test_atomic_write_failure_preserves_previous_file(monkeypatch, tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("original")
    def fail(*_):
        raise OSError("simulated disk error")
    monkeypatch.setattr("src.artifacts.os.replace", fail)
    with pytest.raises(OSError):
        atomic_write_text(p, "replacement")
    assert p.read_text() == "original"
    assert len(list(tmp_path.iterdir())) == 1


def test_local_pipeline_failure_returns_nonzero(monkeypatch, tmp_path):
    from src.summarizer import Summarizer
    media = tmp_path / "courses" / "12345-Test" / "录屏" / "lecture_123456.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"not a video")
    monkeypatch.setattr(Summarizer, "__init__", lambda self: None)
    monkeypatch.setenv("ICOURSE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("sys.argv", ["icourse", "--mode", "summarize", "--course-ids", "12345",
                                    "--out-dir", str(tmp_path / "courses"), "--summary-dir", str(tmp_path / "notes")])
    assert d.main() == 1
