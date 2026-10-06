"""Actual loopback HTTP streaming, synthetic bytes and isolated indexes only."""
import hashlib
import json
import socket
import threading
from collections import Counter
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import Mock

import pytest

from src.elearning_helper import sync
from src.elearning_helper.api import CanvasClient, LoginRequired
from src.elearning_helper.auth import MemorySchoolSession
from src.elearning_helper.demo import DemoClient, DemoResponse, demo_config
from src.elearning_helper.state import Store


@pytest.fixture
def loopback(tmp_path, monkeypatch):
    state = SimpleNamespace(bodies={1: b'healthy file', 2: b'x' * 1181044}, hits=Counter(),
        partial=8192, fail_count=1, mode='eof', fragments=1, ranges=[], temp_names=[], pauses=[])
    config = demo_config(tmp_path)
    course = config.courses[0]
    monkeypatch.setattr(sync.time, 'sleep', state.pauses.append)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            path = urlsplit(self.path).path
            if '/assignments' in path:
                body = b'[]'
            elif '/files' in path:
                body = json.dumps([{'id': fid, 'display_name': f'fixture-{fid}.bin',
                    'size': len(data), 'modified_at': 'v1',
                    'url': state.base + f'/download/{fid}?signature=SECRET_SIGNED_URL'}
                    for fid, data in state.bodies.items()]).encode()
            else:
                fid = int(path.rsplit('/', 1)[1])
                state.hits[fid] += 1
                state.ranges.append(self.headers.get('Range'))
                state.temp_names.append([p.name for p in course.directory.glob('.elearning-*.part')])
                body = state.bodies[fid]
                failing = fid == 2 and state.hits[fid] <= state.fail_count
                self.send_response(200)
                self.send_header('Content-Encoding', 'identity')
                if failing and state.mode == 'chunked':
                    self.send_header('Transfer-Encoding', 'chunked')
                    self.end_headers()
                    # Advertise a complete chunk, then disconnect inside it.
                    self.wfile.write(f'{len(body):X}\r\n'.encode() + body[:state.partial])
                else:
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    if failing:
                        self.wfile.write(body[:state.partial])
                    else:
                        step = max(1, len(body) // state.fragments)
                        for start in range(0, len(body), step):
                            self.wfile.write(body[start:start+step])
                self.wfile.flush()
                if failing:
                    self.connection.shutdown(socket.SHUT_WR)
                    self.close_connection = True
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    state.base = f'http://127.0.0.1:{server.server_port}'
    state.config = replace(config, base_url=state.base)
    state.store = Store(config.state_dir / 'index.sqlite3')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        state.store.close()
        server.shutdown()
        server.server_close()
        thread.join()


def client_for(state, transport):
    if transport == 'session':
        session = MemorySchoolSession()
        session.base = state.base  # Synthetic authenticated session, no login or saved cookies.
        session.authenticated = True
        return CanvasClient(state.base, session=session, allow_loopback=True)
    return CanvasClient(state.base, 'FAKE_LOCAL_TOKEN', allow_loopback=True)


@pytest.mark.parametrize('transport', ['session', 'token'])
@pytest.mark.parametrize('size,partial,fragments,mode', [
    (1181044, 8192, 1, 'eof'), (1729948, 217088, 53, 'eof'), (600123, 8192, 17, 'chunked')])
def test_real_short_stream_recovers_only_failed_file_and_repeat_uses_index(loopback, capsys, transport, size, partial, fragments, mode):
    s = loopback
    s.bodies[2] = b'x' * size
    s.partial, s.fragments, s.mode = partial, fragments, mode
    client = client_for(s, transport)
    try:
        result = sync.run_sync(client, s.store, s.config)[0]
        good, recovered = result['files']
        assert good['status'] == recovered['status'] == 'downloaded'
        assert s.hits == {1: 1, 2: 2} and s.pauses == [1]
        assert recovered['download_attempts'] == 2
        assert recovered['downloaded_bytes'] == size + partial
        assert Path(recovered['path']).read_bytes() == s.bodies[2]
        assert s.store.file(s.config.courses[0].id, '2')['sha256'] == hashlib.sha256(s.bodies[2]).hexdigest()
        assert all(len(names) == 1 for names in s.temp_names)
        assert len({names[0] for names in s.temp_names}) == 3
        assert not list(s.config.courses[0].directory.glob('.elearning-*.part'))
        assert s.ranges == [None] * 3
        repeated = sync.run_sync(client, s.store, s.config)[0]
        assert all(item['status'] == 'name_preserved' and item['downloaded_bytes'] == 0 for item in repeated['files'])
        assert s.hits == {1: 1, 2: 2}
        log = capsys.readouterr().err
        assert 'attempt=1/3' in log and f'清单={size}' in log and f'实收={partial}' in log
        assert 'SECRET' not in log and 'http://' not in log and 'FAKE_LOCAL_TOKEN' not in log
    finally:
        client.close()


def test_retry_exhaustion_cleans_partial_files_and_keeps_successful_index(loopback, capsys):
    s = loopback
    s.fail_count = 10
    client = client_for(s, 'session')
    try:
        result = sync.run_sync(client, s.store, s.config)[0]
        assert [item['status'] for item in result['files']] == ['downloaded', 'failed']
        assert s.hits == {1: 1, 2: 3} and s.pauses == [1, 2]
        assert s.store.file(s.config.courses[0].id, '1') is not None
        assert s.store.file(s.config.courses[0].id, '2') is None
        assert not list(s.config.courses[0].directory.glob('.elearning-*.part'))
        assert len(list(s.config.courses[0].directory.iterdir())) == 1
        assert 'attempt=3/3' in result['files'][1]['reason']
        log = capsys.readouterr().err + result['files'][1]['reason']
        assert 'SECRET' not in log and 'http://' not in log
        s.fail_count = 0
        recovered = sync.run_sync(client, s.store, s.config)[0]
        assert recovered['files'][0]['downloaded_bytes'] == 0
        assert recovered['files'][1]['status'] == 'downloaded'
        assert s.hits == {1: 1, 2: 4}
    finally:
        client.close()


def test_cancel_during_retry_backoff_never_retries_or_loses_prior_success(loopback, monkeypatch):
    s = loopback
    def cancel(_): raise KeyboardInterrupt()
    monkeypatch.setattr(sync.time, 'sleep', cancel)
    client = client_for(s, 'session')
    try:
        with pytest.raises(KeyboardInterrupt):
            sync.run_sync(client, s.store, s.config)
        assert s.hits == {1: 1, 2: 1}
        assert s.store.file(s.config.courses[0].id, '1') is not None
        assert s.store.file(s.config.courses[0].id, '2') is None
        assert not list(s.config.courses[0].directory.glob('.elearning-*.part'))
    finally:
        client.close()


@pytest.mark.parametrize('failure', [TimeoutError('SECRET_URL'), ConnectionResetError('SECRET_URL')])
def test_stream_exception_gets_fresh_response_without_leaking_error(tmp_path, monkeypatch, capsys, failure):
    config = demo_config(tmp_path)
    client = DemoClient()
    file = client.file_rows[0]
    first = DemoResponse(client.bodies[file['url']])
    first.read = Mock(side_effect=[b'abc', failure])
    responses = [first, DemoResponse(client.bodies[file['url']])]
    client.open = Mock(side_effect=responses)
    pauses = []
    monkeypatch.setattr(sync.time, 'sleep', pauses.append)
    store = Store(config.state_dir / 'index.sqlite3')
    try:
        result = sync.sync_file(client, store, config, config.courses[0], file, dry_run=False)
        assert result['download_attempts'] == 2 and client.open.call_count == 2 and pauses == [1]
        assert first.closed and responses[1].closed
        assert result['downloaded_bytes'] == file['size'] + 3
        assert '实收=3' in capsys.readouterr().err
    finally:
        store.close()


@pytest.mark.parametrize('failure', [KeyboardInterrupt(), LoginRequired('fixture login required')])
def test_cancellation_and_login_errors_are_never_retried(tmp_path, monkeypatch, failure):
    config = demo_config(tmp_path)
    client = DemoClient()
    response = DemoResponse(client.bodies[client.file_rows[0]['url']])
    response.read = Mock(side_effect=failure)
    client.open = Mock(return_value=response)
    monkeypatch.setattr(sync.time, 'sleep', lambda _: pytest.fail('must not retry'))
    store = Store(config.state_dir / 'index.sqlite3')
    try:
        with pytest.raises(type(failure)):
            sync.sync_file(client, store, config, config.courses[0], client.file_rows[0], dry_run=False)
        assert client.open.call_count == 1 and response.closed
        assert list(config.courses[0].directory.iterdir()) == []
    finally:
        store.close()
