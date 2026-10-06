"""All-file transfer fixtures are inert, temporary, and never executed/opened."""
import hashlib
import io
import json
import threading
import zipfile
from dataclasses import replace
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.elearning_helper.api import CanvasClient
from src.elearning_helper.config import HARD_LIMIT, load_config
from src.elearning_helper.demo import DemoClient, DemoResponse, demo_config
from src.elearning_helper.state import Store
from src.elearning_helper.sync import FileRejected, eligible, run_sync, safe_name, sync_file


@pytest.fixture
def setup(tmp_path):
    config = demo_config(tmp_path)
    client = DemoClient()
    store = Store(config.state_dir/'index.sqlite3')
    try:
        yield config, client, store
    finally:
        store.close()


@pytest.mark.parametrize('name', ['教材.pdf', '讲义.ppt', '作业.docx', '软件包.zip', '代码.py',
    '照片.jpg', '课程网页.html', '视频.mp4', '程序.exe', '安装包.dmg', '无后缀', '数据.csv', '空文件'])
def test_all_types_save_opaque_bytes_without_execution_or_extraction(setup, name):
    config, client, store = setup
    course = config.courses[0]
    body = b'' if name == '空文件' else b'arbitrary inert bytes: ' + name.encode()
    file = {**client.file_rows[0], 'display_name': name, 'size': len(body), 'content-type': 'anything/custom'}
    client.open = Mock(return_value=DemoResponse(body, 'arbitrary/mime'))
    result = sync_file(client, store, config, course, file, dry_run=False)
    path = Path(result['path'])
    assert path.read_bytes() == body and path.stat().st_mode & 0o111 == 0
    assert list(course.directory.iterdir()) == [path]
    assert store.file(course.id, str(file['id']))['sha256'] == hashlib.sha256(body).hexdigest()
    again = sync_file(client, store, config, course, file, dry_run=False)
    assert again['downloaded_bytes'] == 0 and client.open.call_count == 1


@pytest.mark.parametrize('size, accepted', [(0, True), (49_999_999, True), (50_000_000, False),
    (50_000_001, False), (50*1024*1024, False), (None, False), (-1, False), (True, False)])
def test_exact_decimal_exclusive_boundary_and_missing_metadata(setup, size, accepted):
    config, client, store = setup
    file = {**client.file_rows[0], 'size': size, 'display_name': 'arbitrary.zip'}
    assert (eligible(file, config.courses[0], config) is None) == accepted


@pytest.mark.parametrize('flag', [{'locked_for_user': True}, {'locked': True}, {'hidden_for_user': True},
    {'published': False}, {'workflow_state': 'unpublished'}])
def test_inaccessible_files_never_request_body(setup, flag):
    config, client, store = setup
    client.file_rows = [{**client.file_rows[0], **flag}]
    client.open = Mock(side_effect=AssertionError('restricted content must not be requested'))
    result = run_sync(client, store, config)
    assert result[0]['files'][0]['status'] == 'skipped' and not client.open.called


class GeneratedResponse:
    def __init__(self, length, declared=None):
        self.remaining = length
        self.headers = Message()
        self.status = 200
        if declared is not None: self.headers['Content-Length'] = str(declared)
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, maximum):
        count = min(maximum, self.remaining)
        self.remaining -= count
        return b'x' * count


@pytest.mark.parametrize('actual', [49_999_999, 50_000_000])
def test_real_fifty_million_boundary_without_content_length(setup, actual):
    config, client, store = setup
    file = {**client.file_rows[0], 'display_name': 'boundary.bin', 'size': 49_999_999}
    client.open = Mock(return_value=GeneratedResponse(actual))
    if actual < HARD_LIMIT:
        result = sync_file(client, store, config, config.courses[0], file, dry_run=False)
        assert Path(result['path']).stat().st_size == actual
    else:
        with pytest.raises(FileRejected, match='达到或超过'):
            sync_file(client, store, config, config.courses[0], file, dry_run=False)
        assert list(config.courses[0].directory.iterdir()) == [] and store.file(config.courses[0].id, '1') is None


def test_exact_header_limit_is_rejected_before_body(setup):
    config, client, store = setup
    response = GeneratedResponse(HARD_LIMIT, declared=HARD_LIMIT)
    response.read = Mock(side_effect=AssertionError('must not read oversized response'))
    client.open = Mock(return_value=response)
    file = {**client.file_rows[0], 'size': HARD_LIMIT - 1}
    with pytest.raises(FileRejected, match='上限'):
        sync_file(client, store, config, config.courses[0], file, dry_run=False)


def test_ordinary_html_and_html_in_source_code_are_not_login_pages(setup):
    config, client, store = setup
    bodies = [b'<!doctype html><html><h1>Course webpage</h1><script>throw "not executed"</script></html>',
              b'print(\'<html><title>Canvas Login</title><form><input type="password"></form></html>\')']
    for index, body in enumerate(bodies):
        file = {**client.file_rows[0], 'id': 100+index, 'display_name': ['example.html', 'example.py'][index], 'size': len(body)}
        client.open = Mock(return_value=DemoResponse(body, 'text/html'))
        result = sync_file(client, store, config, config.courses[0], file, dry_run=False)
        assert Path(result['path']).read_bytes() == body


def test_subject_folders_inside_courseware_scope_are_allowed_for_dedup(setup):
    config, client, store = setup
    course = config.courses[0]
    file = client.file_rows[0]
    existing = course.directory / '作业' / '代码' / '教材.bin'
    existing.parent.mkdir(parents=True)
    existing.write_bytes(client.bodies[file['url']])
    result = sync_file(client, store, config, course, file, dry_run=False)
    assert result['status'] == 'duplicate' and result['path'] == str(existing)


def test_old_extension_and_name_filters_cannot_restore_restrictions(tmp_path):
    data = json.loads((Path(__file__).parents[1]/'src/elearning_helper/config.json').read_text())
    data.update(root=str(tmp_path/'courses'), extensions=['.pdf'], exclude_name_patterns=['.*'], include_name_patterns=['impossible'])
    path = tmp_path/'config.json'; path.write_text(json.dumps(data))
    config = load_config(path)
    assert eligible({'size': 10, 'display_name': '作业程序.zip'}, config.courses[0], config) is None


def test_long_unicode_extension_keeps_name_under_filesystem_limit():
    result = safe_name('../' + '名'*230 + '.' + '字'*230)
    assert len(result.encode()) <= 200 and '/' not in result


def test_local_http_roundtrip_for_multiple_types_and_repeat(tmp_path):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as z: z.writestr('../should-not-extract.py', 'raise RuntimeError()')
    bodies = [('notes.pdf', b'%PDF-fixture'), ('slides.ppt', b'office fixture'), ('work.docx', b'doc fixture'),
              ('archive.zip', archive.getvalue()), ('program.py', b'raise RuntimeError("never execute")'),
              ('photo.jpg', b'image fixture'), ('page.html', b'<html><h1>Course page</h1></html>')]
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            hits.append(self.path)
            if '/assignments?' in self.path: body, mime = b'[]', 'application/json'
            elif '/files?' in self.path:
                body = json.dumps([{'id': i+1, 'display_name': name, 'size': len(payload), 'modified_at': 'v1',
                                   'url': base+'/download/'+str(i)} for i,(name,payload) in enumerate(bodies)]).encode()
                mime = 'application/json'
            elif self.path.startswith('/download/'):
                body = bodies[int(self.path.rsplit('/',1)[1])][1]
                mime = 'text/html' if self.path.endswith('/6') else 'application/octet-stream'
            else: self.send_error(404); return
            self.send_response(200); self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    base = f'http://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    config = replace(demo_config(tmp_path),base_url=base)
    store = Store(config.state_dir/'index.sqlite3')
    client = CanvasClient(base,'FAKE_LOCAL_TEST_TOKEN',allow_loopback=True)
    try:
        first = run_sync(client,store,config)
        assert all(r['status']=='downloaded' for r in first[0]['files'])
        second = run_sync(client,store,config)
        assert all(r['status']=='name_preserved' and r['downloaded_bytes']==0 for r in second[0]['files'])
        assert len([p for p in hits if p.startswith('/download/')])==len(bodies)
        assert not (config.courses[0].directory.parent/'should-not-extract.py').exists()
        assert all(not p.stat().st_mode & 0o111 for p in config.courses[0].directory.iterdir())
    finally:
        store.close();client.close();server.shutdown();server.server_close();thread.join()
