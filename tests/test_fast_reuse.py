import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.artifacts import _cached_hash, cached_file_sha256
from src.pipeline_state import artifact_metadata, valid_artifact


def test_verified_media_hash_survives_process_restart(tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    monkeypatch.setenv('ICOURSE_HASH_CACHE_DIR', str(cache))
    media = tmp_path / 'lecture.mp4'
    media.write_bytes(b'a' * (8 * 1024 * 1024))
    expected = cached_file_sha256(media)
    # A new process has no in-memory LRU entries and must use the saved record.
    code = '''import sys
import src.artifacts as a
a.file_sha256=lambda *_: (_ for _ in ()).throw(AssertionError('Unexpected whole-video read'))
assert a.cached_file_sha256(sys.argv[1]) == sys.argv[2]
'''
    subprocess.run([sys.executable, '-c', code, str(media), expected], check=True, env=dict(os.environ))
    original_time = media.stat().st_mtime_ns
    with media.open('r+b') as stream:
        stream.write(b'b')
    os.utime(media, ns=(media.stat().st_atime_ns, original_time))
    assert cached_file_sha256(media) != expected  # ctime detects same-size/backdated edits.


def test_corrupt_cache_and_mid_read_changes_are_not_trusted(tmp_path, monkeypatch):
    monkeypatch.setenv('ICOURSE_HASH_CACHE_DIR', str(tmp_path / 'cache'))
    media = tmp_path / 'lecture.mp4'
    media.write_bytes(b'a' * (8 * 1024 * 1024))
    expected = cached_file_sha256(media)
    next((tmp_path / 'cache').glob('*.json')).write_text('broken')
    _cached_hash.cache_clear()
    assert cached_file_sha256(media) == expected
    _cached_hash.cache_clear()
    next((tmp_path / 'cache').glob('*.json')).unlink()
    def changed(path):
        Path(path).write_bytes(b'different')
        return expected
    monkeypatch.setattr('src.artifacts.file_sha256', changed)
    with pytest.raises(RuntimeError, match='校验期间'):
        cached_file_sha256(media)
    assert not list((tmp_path / 'cache').glob('*.json'))


def test_user_edited_finished_notes_are_not_regenerated(tmp_path):
    transcript = tmp_path / 'lecture.txt'
    transcript.write_text('lecture content')
    artifact_metadata(transcript)
    notes = tmp_path / 'lecture.md'
    notes.write_text('generated notes')
    artifact_metadata(notes, source=transcript, status='complete')
    notes.write_text('student annotations and revised notes')
    assert valid_artifact(notes)
    transcript.write_text('different source')
    assert not valid_artifact(notes)


def test_explicit_verification_bypasses_persistent_hash(tmp_path, monkeypatch):
    monkeypatch.setenv('ICOURSE_HASH_CACHE_DIR', str(tmp_path / 'cache'))
    media = tmp_path / 'lecture.mp4'
    media.write_bytes(b'a' * (8 * 1024 * 1024))
    expected = cached_file_sha256(media)
    calls = []
    def verify(path):
        calls.append(path)
        return expected
    monkeypatch.setattr('src.artifacts.file_sha256', verify)
    _cached_hash.cache_clear()
    assert cached_file_sha256(media) == expected
    assert not calls
    monkeypatch.setenv('ICOURSE_VERIFY_FILES', '1')
    assert cached_file_sha256(media) == expected
    assert len(calls) == 1
