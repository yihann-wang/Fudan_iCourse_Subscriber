"""Both desktop modes fill only the requested missing artifacts, offline."""
import io
import json
import wave

import pytest

from src.asr import ASRSettings, Segment, TranscriptionResult
from src.pipeline import main
from src.pipeline_state import artifact_metadata
from src.summary_result import SummaryResult


@pytest.mark.parametrize('mode', ['download', 'download_and_summarize'])
@pytest.mark.parametrize('have_video', [False, True])
@pytest.mark.parametrize('have_notes', [False, True])
@pytest.mark.parametrize('have_transcript', [False, True])
def test_fill_missing_artifacts_without_rebilling_existing_results(
        tmp_path, monkeypatch, capsys, mode, have_video, have_notes, have_transcript):
    from src.icourse import ICourseClient
    from src.summarizer import Summarizer
    from src.transcriber import Transcriber
    from src.task_view_model import TaskViewModel
    root = tmp_path / 'courses'
    video = root / '12345-Test/录屏/课_123456.mp4'
    text = root / '12345-Test/原始txt/课_123456.txt'
    note = root / '12345-Test/笔记/课_123456.md'
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\0\0' * 16000)
    content = stream.getvalue()
    # Source path no longer exists after migration; content identity still matches.
    old_video = tmp_path / 'removed-drive' / video.name
    old_video.parent.mkdir()
    old_video.write_bytes(content)
    if have_video:
        video.parent.mkdir(parents=True)
        video.write_bytes(content)
    if have_transcript:
        text.parent.mkdir(parents=True)
        text.write_text('已有完整转录')
        artifact_metadata(text, source=old_video)
    if have_notes:
        note.parent.mkdir(parents=True)
        note.write_text('已完成的笔记')
        artifact_metadata(note, source=text if have_transcript else old_video, status='complete')
        note.write_text('保留用户编辑过的笔记')
    old_video.unlink()
    calls = dict(download=0, asr=0, notes=0)

    class Response:
        status_code = 200
        headers = {'content-type': 'video/mp4', 'content-length': str(len(content))}
        def raise_for_status(self):
            pass
        def iter_content(self, chunk_size):
            yield content
        def close(self):
            pass

    class VPN:
        def get(self, *args, **kwargs):
            calls['download'] += 1
            return Response()

    monkeypatch.setattr('src.pipeline._login_with_retry', lambda *a, **k: VPN())
    monkeypatch.setattr(ICourseClient, 'get_course_detail', lambda *a: dict(title='Test', lectures=[
        dict(sub_id='123456', sub_title='课', has_playback=True)]))
    monkeypatch.setattr(ICourseClient, 'get_video_url', lambda *a: 'https://example.invalid/recording')
    monkeypatch.setattr(Summarizer, '__init__', lambda self: None)

    def transcribe(self, *args, **kwargs):
        calls['asr'] += 1
        self._last_segments = [(0, 1, '新转录')]
        settings = ASRSettings.from_env()
        return TranscriptionResult('新转录', (Segment(0, 1, '新转录'),), 'zh', 1, 1,
                                   'cloud', settings.model, '', True, 1, settings.fingerprint)

    def summarize(self, title, text_value, **kwargs):
        calls['notes'] += 1
        assert text_value == ('已有完整转录' if have_transcript else '新转录')
        return SummaryResult('新笔记', 'fixture')

    monkeypatch.setattr(Transcriber, 'transcribe_result', transcribe)
    monkeypatch.setattr(Summarizer, 'summarize', summarize)
    monkeypatch.setenv('StuId', 'test')
    monkeypatch.setenv('UISPsw', 'test')
    monkeypatch.setenv('ICOURSE_EVENTS', 'json')
    monkeypatch.delenv('ICOURSE_RUN_ID', raising=False)
    monkeypatch.setattr('sys.argv', ['icourse', '--mode', mode, '--course-ids', '12345',
        '--out-dir', str(root), '--summary-dir', str(root), '--sleep', '0'])
    assert main() == 0
    need_notes = mode != 'download' and not have_notes
    assert calls == dict(download=int(not have_video), asr=int(need_notes and not have_transcript), notes=int(need_notes))
    assert video.read_bytes() == content
    if have_notes:
        assert note.read_text() == '保留用户编辑过的笔记'
    if have_transcript:
        assert text.read_text() == '已有完整转录'
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{"event": "icourse"')]
    model = TaskViewModel(rows[0]['run_id'])
    for row in rows:
        model.apply(row)
    model.finish(0)
    expected = 'skipped' if mode == 'download' and have_video else 'success'
    assert model.counts()[expected] == 1
    before = dict(calls)
    assert main() == 0
    assert calls == before


def test_relocated_transcript_must_match_new_video_contents(tmp_path):
    from src.pipeline_state import valid_artifact
    video, text = tmp_path / 'old.mp4', tmp_path / 'text.txt'
    video.write_bytes(b'original recording')
    text.write_text('complete transcript')
    artifact_metadata(text, source=video)
    video.unlink()
    new_video = tmp_path / 'new.mp4'
    new_video.write_bytes(b'different recording')
    assert not valid_artifact(text, source=new_video)
    new_video.write_bytes(b'original recording')
    assert valid_artifact(text, source=new_video)
