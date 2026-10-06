"""Safe diagnostics use synthetic in-memory HTTP responses; never school traffic."""
import http.client
from unittest.mock import Mock

import pytest

from src.elearning_helper.demo import DemoClient, DemoResponse, demo_config
from src.elearning_helper.state import Store
from src.elearning_helper.sync import FileRejected, sync_file, run_sync


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("src.elearning_helper.sync.time.sleep", lambda _: None)
    config = demo_config(tmp_path)
    client = DemoClient()
    store = Store(config.state_dir / "index.sqlite3")
    try:
        yield config, client, store
    finally:
        store.close()


@pytest.mark.parametrize("mode", ["eof", "header_mismatch", "gzip", "partial", "content_range", "exception", "timeout"])
def test_failed_transfer_has_bounded_safe_diagnostics_and_never_publishes(setup, mode):
    config, client, store = setup
    course = config.courses[0]
    file = client.file_rows[0]
    def fresh_response(*args, **kwargs):
        response = DemoResponse(b"short", content_length=False)
        response.status = 200
        response.headers['Set-Cookie'] = 'SECRET_COOKIE'
        response.headers['Location'] = 'https://example.test/?signature=SECRET_URL'
        response.headers['X-Trace'] = 'SECRET_TRACE'
        if mode == 'header_mismatch': response.headers['Content-Length'] = '5'
        if mode == 'gzip': response.headers['Content-Encoding'] = 'gzip'
        if mode == 'partial': response.status = 206
        if mode == 'content_range': response.headers['Content-Range'] = 'bytes 0-4/999'
        if mode == 'exception': response.read = Mock(side_effect=http.client.IncompleteRead(b'abc', 9))
        if mode == 'timeout': response.read = Mock(side_effect=TimeoutError('SECRET_URL'))
        if mode in {'header_mismatch', 'gzip', 'partial', 'content_range'}:
            response.read = Mock(side_effect=AssertionError('body must not be read'))
        return response
    client.open = Mock(side_effect=fresh_response)
    with pytest.raises(FileRejected) as caught:
        sync_file(client, store, config, course, file, dry_run=False)
    message = str(caught.value)
    assert f'清单={file["size"]}' in message and '实收=' in message and 'HTTP=' in message
    assert 'SECRET' not in message and 'https:' not in message
    assert client.open.call_count == (3 if mode in {"eof", "exception", "timeout"} else 1)
    assert len(message) <= 240
    if mode == 'eof': assert '实收=5' in message and 'Content-Length=缺失/无效' in message
    if mode == 'exception': assert '实收=3' in message
    assert store.file(course.id, '1') is None and list(course.directory.iterdir()) == []


def test_successful_index_survives_another_file_failure_and_retry_skips_it(setup):
    config, client, store = setup
    course = config.courses[0]
    good, bad = client.file_rows[:2]
    original_open = client.open
    calls = []
    def controlled_open(url, **kwargs):
        calls.append(url)
        return DemoResponse(b'short', content_length=False) if url == bad['url'] else original_open(url, **kwargs)
    client.open = controlled_open
    run_sync(client, store, config)
    assert store.file(course.id, str(good['id'])) is not None
    assert store.file(course.id, str(bad['id'])) is None
    assert store.course(course.id)['files_checked_at'] is None
    calls.clear()
    second = run_sync(client, store, config)
    assert calls == [bad['url']] * 3
    assert second[0]['files'][0]['downloaded_bytes'] == 0


def test_same_name_preservation_does_not_leave_course_incomplete(setup):
    config, client, store = setup
    course = config.courses[0]
    course.directory.mkdir(parents=True)
    (course.directory / client.file_rows[0]['display_name']).write_bytes(b'possible annotations')
    result = run_sync(client, store, config)
    assert result[0]['files'][0]['status'] == 'name_preserved'
    assert store.course(course.id)['files_checked_at'] is not None
