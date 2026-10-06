"""Preserve user bytes by safe filename only; never invent remote hash identity."""
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.elearning_helper import sync
from src.elearning_helper.demo import DemoClient, demo_config
from src.elearning_helper.state import Store


@pytest.fixture
def setup(tmp_path):
    config = demo_config(tmp_path)
    course = config.courses[0]
    course.directory.mkdir(parents=True)
    client = DemoClient()
    store = Store(config.state_dir / 'index.sqlite3')
    try:
        yield config, course, client, store
    finally:
        store.close()


@pytest.mark.parametrize('remote,local', [('Notes.PDF', 'notes.pdf'), ('café.pdf', 'cafe\u0301.PDF'),
                                         ('../lesson.pdf', sync.safe_name('../lesson.pdf'))])
def test_unindexed_normalized_name_preserves_bytes_without_hash_network_or_false_index(setup, monkeypatch, remote, local):
    config, course, client, store = setup
    file = {**client.file_rows[0], 'display_name': remote}
    original = course.directory / '章节' / local
    original.parent.mkdir()
    original.write_bytes(b'local annotations different from remote')
    before = original.stat()
    monkeypatch.setattr(sync, 'sha256', lambda *_: pytest.fail('same-name policy must not hash'))
    client.open = Mock(side_effect=AssertionError('same-name policy must not transfer'))
    for dry_run, compare in [(True, False), (False, False), (False, True)]:
        result = sync.sync_file(client, store, config, course, file, dry_run=dry_run, compare_existing=compare)
        assert result['status'] == 'name_preserved' and result['path'] == str(original)
        assert result['downloaded_bytes'] == 0 and result['local_content_verified'] is False
        assert result['source_file_id'] == str(file['id']) and result['source_bytes'] == file['size']
        assert store.file(course.id, str(file['id'])) is None
    assert not client.open.called
    assert original.read_bytes() == b'local annotations different from remote'
    assert original.stat().st_mtime_ns == before.st_mtime_ns
    assert list(course.directory.rglob('*')) == [original.parent, original]


@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_indexed_annotations_and_same_name_remote_update_never_advance_verified_index(setup, monkeypatch, version):
    config, course, client, store = setup
    file = client.file_rows[0]
    first = sync.sync_file(client, store, config, course, file, dry_run=False)
    previous = dict(store.file(course.id, str(file['id'])))
    original = Path(first['path'])
    original.write_bytes(b'important handwritten notes')
    client.open = Mock(side_effect=AssertionError('do not download same-name remote revisions'))
    monkeypatch.setattr(sync, 'sha256', lambda *_: pytest.fail('do not hash same-name notes'))
    result = sync.sync_file(client, store, config, course, {**file, 'modified_at': version, 'size': 17}, dry_run=False)
    assert result['status'] == 'name_preserved' and result['downloaded_bytes'] == 0
    assert dict(store.file(course.id, str(file['id']))) == previous
    assert original.read_bytes() == b'important handwritten notes'
    assert list(course.directory.iterdir()) == [original]


@pytest.mark.parametrize('kind', ['directory', 'symlink', 'fifo'])
def test_nonregular_same_name_target_is_not_followed_preserved_as_verified_or_downloaded(setup, monkeypatch, kind):
    config, course, client, store = setup
    file = client.file_rows[0]
    target = course.directory / file['display_name']
    external = course.directory.parent / 'external.pdf'
    external.write_bytes(b'outside bytes must remain untouched')
    if kind == 'directory': target.mkdir()
    elif kind == 'symlink': target.symlink_to(external)
    else: os.mkfifo(target)
    client.open = Mock(side_effect=AssertionError('unsafe target must fail before network'))
    monkeypatch.setattr(sync, 'sha256', lambda *_: pytest.fail('must not read nonregular target'))
    with pytest.raises(sync.FileRejected, match='不是普通文件'):
        sync.sync_file(client, store, config, course, file, dry_run=False)
    assert not client.open.called and store.file(course.id, str(file['id'])) is None
    assert external.read_bytes() == b'outside bytes must remain untouched'
    assert list(course.directory.iterdir()) == [target]


@pytest.mark.parametrize('when', ['during_download', 'atomic_publish'])
def test_concurrent_same_name_creation_never_overwrites_versions_or_falsifies_hash(setup, monkeypatch, when):
    config, course, client, store = setup
    file = client.file_rows[0]
    target = course.directory / file['display_name']
    if when == 'during_download':
        original_open = client.open
        def opened(*args, **kwargs):
            target.write_bytes(b'concurrent user notes')
            return original_open(*args, **kwargs)
        client.open = opened
    else:
        link = sync.os.link
        def racing_link(src, dst):
            target.write_bytes(b'concurrent user notes')
            return link(src, dst)
        monkeypatch.setattr(sync.os, 'link', racing_link)
    result = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert result['status'] == 'name_preserved' and not result['local_content_verified']
    assert result['downloaded_bytes'] == file['size']  # Transfer began before the file existed.
    assert target.read_bytes() == b'concurrent user notes'
    assert list(course.directory.iterdir()) == [target]
    assert store.file(course.id, str(file['id'])) is None
