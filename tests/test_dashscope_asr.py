"""Offline contract tests: both file-ASR models, OSS isolation and task recovery."""
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.asr.client import CloudWorker
from src.asr.cloud import SpeechAPIError
from src.asr.dashscope import DashScopeAPI, _is_empty_recognition, parse_dashscope_result
from src.asr.types import ASRSettings, DASHSCOPE_BASE_URL
from src.preferences import defaults, runtime_environment
from src.transcriber import write_srt


@pytest.fixture
def ali_server():
    state = dict(requests=[], responses=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.do_POST()

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            state['requests'].append((self.command, self.path, self.headers, body))
            code, payload = state['responses'].pop(0)
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    state['url'] = f'http://127.0.0.1:{http.server_port}'
    yield state
    http.shutdown()
    http.server_close()
    thread.join(timeout=2)


def policy(server):
    return {'data': dict(upload_host=server['url'] + '/oss', upload_dir='private/test',
                        oss_access_key_id=uuid4().hex, policy=uuid4().hex, signature=uuid4().hex,
                        x_oss_object_acl='private', x_oss_forbid_overwrite='true', max_file_size_mb=20)}


def transcript(text='这是课程内容。', duration=3):
    return {'properties': {'original_duration_in_milliseconds': duration * 1000},
            'transcripts': [{'channel_id': 0, 'text': text, 'sentences': [
                {'begin_time': 120, 'end_time': 2500, 'text': text}]}]}


def success(server, subtask='SUCCEEDED'):
    return {'output': {'task_status': 'SUCCEEDED', 'results': [
        {'subtask_status': subtask, 'transcription_url': server['url'] + '/result?Signature=private'}]}}


def setup(server, tmp_path, model='fun-asr'):
    media = tmp_path / 'private-lecture.mp3'
    media.write_bytes(b'test-audio')
    settings = ASRSettings(provider='dashscope', base_url=server['url'] + '/api/v1',
                           model=model, api_key=uuid4().hex, language='zh,en', retries=0)
    return media, tmp_path / 'chunk.task.json', settings


@pytest.mark.parametrize('model', ['fun-asr', 'paraformer-v2', 'fun-asr-2025-11-07', 'Vendor/Custom-ASR-v7'])
def test_both_models_upload_poll_and_write_timed_subtitles(ali_server, tmp_path, model):
    media, state, settings = setup(ali_server, tmp_path, model)
    ali_server['responses'] = [(200, policy(ali_server)), (200, {}),
        (200, {'output': {'task_id': 'test-job', 'task_status': 'PENDING'}}),
        (200, success(ali_server)), (200, transcript())]
    worker = CloudWorker(settings)
    try:
        result = worker.request(media, duration=3, task_path=state)
    finally:
        worker.close()
    assert result['text'] == '这是课程内容。'
    assert result['segments'] == [dict(start=.12, end=2.5, text='这是课程内容。')]
    output = tmp_path / 'lecture.srt'
    write_srt([(s['start'], s['end'], s['text']) for s in result['segments']], output)
    assert '00:00:00,120 --> 00:00:02,500' in output.read_text()
    calls = ali_server['requests']
    assert len(calls) == 5
    assert all(calls[i][2].get('Authorization') == 'Bearer ' + settings.api_key for i in (0, 2, 3))
    assert all(calls[i][2].get('Authorization') is None for i in (1, 4))
    assert b'test-audio' in calls[1][3] and b'private-lecture' not in calls[1][3]
    request = json.loads(calls[2][3])
    assert request['model'] == model and request['parameters']['channel_id'] == [0]
    assert request['input']['file_urls'][0].startswith('oss://private/test/')
    assert calls[2][2]['X-DashScope-Async'] == 'enable'
    assert calls[2][2]['X-DashScope-OssResourceResolve'] == 'enable'
    assert request['parameters']['language_hints'] == ['zh', 'en']
    assert 'timestamp_alignment_enabled' not in request['parameters']  # Service default for every model.
    assert 'response_format' not in request and 'prompt' not in request
    saved = state.read_text()
    assert settings.api_key not in saved and 'oss://' not in saved and 'Signature' not in saved
    assert state.stat().st_mode & 0o777 == 0o600
    api = DashScopeAPI(settings)
    try:
        assert api.transcribe(media, 3, task_path=state)['text'] == result['text']
    finally:
        api.close()
    assert len(ali_server['requests']) == 5  # Result survives a parent crash before checkpoint write.


@pytest.mark.parametrize('alignment,expected', [('enabled', True), ('disabled', False)])
def test_timestamp_alignment_is_explicit_and_independent_of_model(ali_server, tmp_path, alignment, expected):
    media, state, settings = setup(ali_server, tmp_path, 'Custom-ASR')
    settings = replace(settings, timestamp_alignment=alignment)
    ali_server['responses'] = [(200, policy(ali_server)), (200, {}),
        (200, {'output': {'task_id': 'custom-job'}}), (200, success(ali_server)), (200, transcript())]
    api = DashScopeAPI(settings)
    try:
        assert api.transcribe(media, 3, task_path=state)['text']
    finally:
        api.close()
    request = json.loads(ali_server['requests'][2][3])
    assert request['parameters']['timestamp_alignment_enabled'] is expected
    assert request['model'] == 'Custom-ASR'
    assert settings.fingerprint != replace(settings, timestamp_alignment='default').fingerprint


def test_custom_gateway_model_and_protocol_mismatch_errors():
    settings = ASRSettings(provider='dashscope', base_url='https://speech.example.invalid/team/api/v1', model='Custom/v9').resolved()
    assert settings.model == 'Custom/v9' and settings.base_url.endswith('/team/api/v1')
    api = DashScopeAPI(settings)
    try:
        assert api._storage_url('https://speech.example.invalid/private/file')
        with pytest.raises(SpeechAPIError):
            api._storage_url('https://unrelated.example.invalid/private/file')
    finally:
        api.close()
    with pytest.raises(ValueError, match='不能使用'):
        replace(settings, base_url='https://dashscope.aliyuncs.com/compatible-mode/v1').resolved()
    with pytest.raises(ValueError, match='切换'):
        replace(settings, provider='openai', base_url=DASHSCOPE_BASE_URL).resolved()


def test_cli_overrides_are_validated_after_protocol_selection(monkeypatch):
    from src.cli import _settings
    monkeypatch.setenv('ASR_PROVIDER', 'openai')
    monkeypatch.setenv('ASR_BASE_URL', DASHSCOPE_BASE_URL)
    monkeypatch.delenv('ASR_BACKEND', raising=False)
    args = SimpleNamespace(provider='dashscope', model='Custom/v9', base_url=None,
                           response_format=None, timestamp_alignment='enabled')
    settings = _settings(args)
    assert settings.model == 'Custom/v9' and settings.timestamp_alignment == 'enabled'


def test_poll_failure_resumes_task_without_reupload_or_rebill(ali_server, tmp_path):
    media, state, settings = setup(ali_server, tmp_path)
    ali_server['responses'] = [(200, policy(ali_server)), (200, {}),
        (200, {'output': {'task_id': 'known-job'}}), (503, {'message': settings.api_key})]
    api = DashScopeAPI(settings)
    try:
        with pytest.raises(SpeechAPIError):
            api.transcribe(media, 3, task_path=state)
        assert json.loads(state.read_text())['task_id'] == 'known-job'
        ali_server['responses'] = [(200, success(ali_server)), (200, transcript())]
        assert api.transcribe(media, 3, task_path=state)['segments']
    finally:
        api.close()
    assert [call[0] for call in ali_server['requests']].count('POST') == 2
    assert ali_server['requests'][4][1] == '/api/v1/tasks/known-job'


def test_uncertain_submission_is_not_automatically_repeated(ali_server, tmp_path):
    media, state, settings = setup(ali_server, tmp_path)
    ali_server['responses'] = [(200, policy(ali_server)), (200, {}), (503, {'message': settings.api_key})]
    api = DashScopeAPI(replace(settings, retries=2))
    try:
        with pytest.raises(SpeechAPIError) as error:
            api.transcribe(media, 3, task_path=state)
        assert settings.api_key not in str(error.value)
        with pytest.raises(SpeechAPIError, match='提交结果不确定'):
            api.transcribe(media, 3, task_path=state)
    finally:
        api.close()
    assert len(ali_server['requests']) == 3


def test_failed_subtask_is_not_accepted_as_success(ali_server, tmp_path):
    media, state, settings = setup(ali_server, tmp_path)
    ali_server['responses'] = [(200, policy(ali_server)), (200, {}),
        (200, {'output': {'task_id': 'failed-job'}}), (200, success(ali_server, 'FAILED'))]
    api = DashScopeAPI(settings)
    try:
        with pytest.raises(SpeechAPIError, match='子任务失败'):
            api.transcribe(media, 3, task_path=state)
    finally:
        api.close()
    assert not state.exists()
    assert len(ali_server['requests']) == 4


@pytest.mark.parametrize('status', ['FAILED', 'CANCELED', 'SUCCEEDED'])
def test_failure_preserves_codes_without_response_secrets(ali_server, tmp_path, status):
    media, state, settings = setup(ali_server, tmp_path)
    unsafe = settings.api_key + ' https://example.invalid/audio?Signature=private source transcript'
    output = dict(task_status=status, code='RecognitionFailed', message=unsafe,
                  results=[dict(subtask_status='FAILED', code='AUDIO_DECODE_FAILED',
                                message=unsafe, file_url=unsafe)])
    ali_server['responses'] = [(200, policy(ali_server)), (200, {}),
        (200, {'output': {'task_id': 'failed-job'}}), (200, {'output': output})]
    api = DashScopeAPI(settings)
    try:
        with pytest.raises(SpeechAPIError, match='AUDIO_DECODE_FAILED') as error:
            api.transcribe(media, 3, task_path=state)
        failure = state.with_suffix('.failure.json')
        data = json.loads(failure.read_text())
        assert data['task_id'] == 'failed-job'
        assert data['error_codes'] == ['RecognitionFailed', 'AUDIO_DECODE_FAILED']
        assert data['task_status'] == ('CANCELED' if status == 'CANCELED' else 'FAILED')
        assert failure.stat().st_mode & 0o777 == 0o600
        combined = str(error.value) + failure.read_text()
        for secret in (settings.api_key, 'https://', 'Signature', 'source transcript'):
            assert secret not in combined
        assert not state.exists()  # A confirmed failure permits an explicit retry.
        ali_server['responses'] = [(200, policy(ali_server)), (200, {}),
            (200, {'output': {'task_id': 'retry-job'}}), (200, success(ali_server)), (200, transcript())]
        assert api.transcribe(media, 3, task_path=state)['text']
        assert not failure.exists()
    finally:
        api.close()


@pytest.mark.parametrize('code', ['https://private.invalid/secret', 'sk-do-not-display', None, 'x' * 101])
def test_invalid_error_codes_are_not_exposed(ali_server, tmp_path, code):
    media, state, settings = setup(ali_server, tmp_path)
    ali_server['responses'] = [(200, policy(ali_server)), (200, {}),
        (200, {'output': {'task_id': 'failed-job'}}),
        (200, {'output': {'task_status': 'FAILED', 'code': code, 'message': settings.api_key}})]
    api = DashScopeAPI(settings)
    try:
        with pytest.raises(SpeechAPIError, match='服务未提供可安全显示的错误码'):
            api.transcribe(media, 3, task_path=state)
    finally:
        api.close()
    assert json.loads(state.with_suffix('.failure.json').read_text())['error_codes'] == []


def test_check_only_requests_upload_policy(ali_server, tmp_path):
    _, _, settings = setup(ali_server, tmp_path)
    ali_server['responses'] = [(200, policy(ali_server))]
    api = DashScopeAPI(settings)
    try:
        assert '未上传音频' in api.check()['message']
    finally:
        api.close()
    assert len(ali_server['requests']) == 1
    assert ali_server['requests'][0][0] == 'GET'


@pytest.mark.parametrize('url', ['https://example.invalid/file', 'http://test.aliyuncs.com/file',
                               'https://test.aliyuncs.com.evil.invalid/file', 'https://u:p@test.aliyuncs.com/file'])
def test_unexpected_storage_destinations_are_rejected(url):
    api = DashScopeAPI(ASRSettings(provider='dashscope', base_url=DASHSCOPE_BASE_URL, model='fun-asr'))
    try:
        with pytest.raises(SpeechAPIError):
            api._storage_url(url)
    finally:
        api.close()


@pytest.mark.parametrize('damage', ['untimed', 'invalid_time', 'truncated', 'missing_sentence', 'wrong_channel'])
def test_missing_or_invalid_timing_is_not_a_completed_result(damage):
    data = transcript()
    if damage == 'untimed':
        del data['transcripts'][0]['sentences'][0]['begin_time']
    elif damage == 'invalid_time':
        data['transcripts'][0]['sentences'][0]['begin_time'] = float('nan')
    elif damage == 'truncated':
        data['properties']['original_duration_in_milliseconds'] = 1
    elif damage == 'missing_sentence':
        data['transcripts'][0]['text'] += '这是丢失的内容'
    else:
        data['transcripts'][0]['channel_id'] = 1
    with pytest.raises(SpeechAPIError):
        parse_dashscope_result(data, 30 if damage == 'truncated' else 3)


def test_long_sentence_uses_word_times_without_inventing_alignment():
    text = '这是课堂内容' * 10
    words = [dict(begin_time=i*200, end_time=(i+1)*200, text=c) for i, c in enumerate(text)]
    data = transcript(text, duration=12)
    sentence = data['transcripts'][0]['sentences'][0]
    sentence.update(begin_time=0, end_time=12000, words=words)
    parsed = parse_dashscope_result(data, 12)
    assert len(parsed['segments']) == 2
    assert ''.join(s['text'] for s in parsed['segments']) == text
    assert all(s['end'] - s['start'] <= 6 for s in parsed['segments'])
    sentence['words'][0]['text'] = '错误'
    parsed = parse_dashscope_result(data, 12)
    assert parsed['segments'] == [dict(start=0, end=12, text=text)]


def test_provider_defaults_and_separate_credentials():
    assert ASRSettings.from_env({'ASR_PROVIDER': 'dashscope'}).base_url == DASHSCOPE_BASE_URL
    values = defaults()
    first, second = uuid4().hex, uuid4().hex
    values.update(asr_api_key=first, asr_dashscope_api_key=second, asr_prompt='existing prompt', asr_response_format='text')
    assert runtime_environment(values, {})['ASR_API_KEY'] == first
    values.update(asr_provider='dashscope', asr_dashscope_model='paraformer-v2')
    env = runtime_environment(values, {})
    assert env['ASR_API_KEY'] == second and first not in env.values()
    assert env['ASR_MODEL'] == 'paraformer-v2' and env['ASR_INITIAL_PROMPT'] == ''
    assert ASRSettings.from_env(env).provider == 'dashscope'
    values['asr_provider'] = 'openai'
    assert runtime_environment(values, {})['ASR_INITIAL_PROMPT'] == 'existing prompt'


def test_english_spacing_survives_word_based_caption_splitting():
    text = 'Kubernetes manages containers across several computing nodes.'
    terms = text.split(' ')
    words = [dict(begin_time=i*1500, end_time=(i+1)*1500, text=t) for i, t in enumerate(terms)]
    data = transcript(text, duration=12)
    data['transcripts'][0]['sentences'][0].update(begin_time=0, end_time=10500, words=words)
    result = parse_dashscope_result(data, 12)
    assert len(result['segments']) >= 2
    assert ' '.join(s['text'] for s in result['segments']) == text


def test_timeout_keeps_task_for_next_attempt(ali_server, tmp_path, monkeypatch):
    media, state, settings = setup(ali_server, tmp_path)
    settings = replace(settings, timeout_seconds=10)
    ali_server['responses'] = [(200, policy(ali_server)), (200, {}),
        (200, {'output': {'task_id': 'slow-job'}}), (200, {'output': {'task_status': 'RUNNING'}})]
    moments = iter([0, 11])
    monkeypatch.setattr('src.asr.dashscope.time', SimpleNamespace(monotonic=lambda: next(moments), sleep=lambda _: None))
    api = DashScopeAPI(settings)
    try:
        with pytest.raises(SpeechAPIError, match='等待超时'):
            api.transcribe(media, 3, task_path=state)
    finally:
        api.close()
    assert json.loads(state.read_text())['task_id'] == 'slow-job'


def test_chunk_merge_offsets_timestamps_and_preserves_completed_audio(tmp_path, monkeypatch):
    import wave
    from src.transcriber import Transcriber
    media = tmp_path / 'recording.wav'
    with wave.open(str(media), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\x10\x10' * (16000 * 65))
    settings = ASRSettings(provider='dashscope', base_url=DASHSCOPE_BASE_URL, model='paraformer-v2', chunk_seconds=30)
    transcriber = Transcriber(settings, cache_dir=tmp_path / 'cache')
    calls = []
    def request(path, *, duration, task_path, **kwargs):
        calls.append(task_path)
        task_path.write_text('{}')
        return dict(text='课堂原文', language='', segments=[dict(start=.2, end=duration-.2, text='课堂原文')])
    monkeypatch.setattr(transcriber._worker, 'request', request)
    try:
        result = transcriber.transcribe_result(media)
        assert [s.start for s in result.segments] == pytest.approx([.2, 30.2, 60.2])
        assert result.segments[-1].end == pytest.approx(64.8)
        assert len(calls) == 3 and not any(p.exists() for p in calls)
        assert transcriber.transcribe_result(media).text == result.text
        assert len(calls) == 3
    finally:
        transcriber.close()


@pytest.mark.parametrize('code', ['ASR_RESPONSE_HAVE_NO_WORDS', 'SUCCESS_WITH_NO_VALID_FRAGMENT'])
@pytest.mark.parametrize('shape', ['task', 'subtask', 'failed_subtask'])
def test_documented_empty_outcome_is_checkpointed_not_rebilled(ali_server, tmp_path, code, shape):
    media, state, settings = setup(ali_server, tmp_path)
    output = dict(task_status='FAILED', code=code)
    if shape != 'task':
        output = dict(task_status='SUCCEEDED' if shape == 'subtask' else 'FAILED',
                      results=[dict(subtask_status='FAILED', code=code)])
    ali_server['responses'] = [(200, policy(ali_server)), (200, {}),
        (200, {'output': {'task_id': 'empty-job'}}), (200, {'output': output})]
    worker = CloudWorker(settings)
    try:
        for _ in range(2):
            result = worker.request(media, duration=3, task_path=state)
            assert result['text'] == '' and result['segments'] == []
        assert len(ali_server['requests']) == 4
        assert json.loads(state.read_text())['phase'] == 'no_words'
    finally:
        worker.close()


@pytest.mark.parametrize('output', [
    dict(task_status='CANCELED', code='ASR_RESPONSE_HAVE_NO_WORDS'),
    dict(task_status='RUNNING', code='ASR_RESPONSE_HAVE_NO_WORDS'),
    dict(task_status='FAILED', code='FILE_CHECK_FAILED', message='ASR_RESPONSE_HAVE_NO_WORDS'),
    dict(task_status='FAILED', code='InvalidApiKey', results=[
        dict(subtask_status='FAILED', code='ASR_RESPONSE_HAVE_NO_WORDS')]),
    dict(task_status='FAILED', results=[dict(subtask_status='FAILED', code='ASR_RESPONSE_HAVE_NO_WORDS'),
                                      dict(subtask_status='FAILED', code='FILE_DOWNLOAD_FAILED')]),
    dict(task_status='FAILED', results=[]),
    dict(task_status='SUCCEEDED', code='ASR_RESPONSE_HAVE_NO_WORDS'),
    dict(task_status='FAILED', code=['ASR_RESPONSE_HAVE_NO_WORDS']),
])
def test_other_failures_or_malformed_results_are_not_empty_recognition(output):
    assert not _is_empty_recognition(output)


def test_previous_no_words_failure_recovers_without_network(ali_server, tmp_path, monkeypatch):
    from src.artifacts import file_sha256
    media, state, settings = setup(ali_server, tmp_path)
    failure = state.with_suffix('.failure.json')
    failure.write_text(json.dumps(dict(fingerprint=settings.fingerprint, audio_sha256=file_sha256(media),
        duration=3, phase='failed', task_status='FAILED', task_id='old-job',
        error_codes=['ASR_RESPONSE_HAVE_NO_WORDS'])))
    api = DashScopeAPI(settings)
    monkeypatch.setattr(api, '_json', lambda *a, **kw: pytest.fail('No new request for confirmed empty result'))
    try:
        assert api.transcribe(media, 3, task_path=state) == dict(text='', segments=[], language='')
        assert json.loads(state.read_text())['task_id'] == 'old-job'
        assert not failure.exists()
    finally:
        api.close()


@pytest.mark.parametrize('change', [dict(fingerprint='other'), dict(audio_sha256='other'), dict(duration=4),
    dict(task_status='CANCELED'), dict(error_codes=['FILE_CHECK_FAILED']), dict(task_id='../unsafe')])
def test_failure_recovery_requires_exact_audio_and_settings(ali_server, tmp_path, change):
    from src.artifacts import file_sha256
    media, state, settings = setup(ali_server, tmp_path)
    failed = dict(fingerprint=settings.fingerprint, audio_sha256=file_sha256(media),
        duration=3, phase='failed', task_status='FAILED', task_id='old-job',
        error_codes=['ASR_RESPONSE_HAVE_NO_WORDS'])
    failed.update(change)
    state.with_suffix('.failure.json').write_text(json.dumps(failed))
    ali_server['responses'] = [(200, policy(ali_server)), (200, {}),
        (200, {'output': {'task_id': 'new-job'}}), (200, success(ali_server)), (200, transcript())]
    api = DashScopeAPI(settings)
    try:
        assert api.transcribe(media, 3, task_path=state)['text']
        assert len(ali_server['requests']) == 5
    finally:
        api.close()


@pytest.mark.parametrize('all_empty', [False, True])
def test_empty_cloud_block_continues_with_visible_gap_and_correct_subtitles(ali_server, tmp_path, all_empty):
    import wave
    from src.transcriber import Transcriber
    media = tmp_path / 'lecture.wav'
    with wave.open(str(media), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\x10\x10' * 16000 * 65)
    _, _, settings = setup(ali_server, tmp_path)
    settings = replace(settings, chunk_seconds=30)
    transcriber = Transcriber(settings, cache_dir=tmp_path / 'cache')
    for index, duration in enumerate([30, 30, 5]):
        replies = [(200, policy(ali_server)), (200, {}), (200, {'output': {'task_id': f'job-{index}'}})]
        if all_empty or index == 1:
            replies.append((200, {'output': dict(task_status='FAILED', code='ASR_RESPONSE_HAVE_NO_WORDS')}))
        else:
            replies.extend([(200, success(ali_server)), (200, transcript(f'第{index + 1}块课堂内容', duration))])
        ali_server['responses'].extend(replies)
    try:
        if all_empty:
            with pytest.raises(RuntimeError, match='未识别到语音'):
                transcriber.transcribe_result(media)
            assert transcriber.last_result is None
            assert not list((tmp_path / 'cache').rglob('*.json'))
        else:
            result = transcriber.transcribe_result(media)
            assert '第1块课堂内容' in result.text and '第3块课堂内容' in result.text
            assert '00:00:30,000–00:01:00,000 未识别到文字' in result.text
            assert any('1 段音频未识别到文字' in warning for warning in result.warnings)
            assert [s.start for s in result.segments] == pytest.approx([.12, 60.12])
            subtitles = tmp_path / 'lecture.srt'
            transcriber.write_srt(subtitles)
            assert '00:01:00,120' in subtitles.read_text() and '未识别' not in subtitles.read_text()
            count = len(ali_server['requests'])
            assert transcriber.transcribe_result(media).text == result.text
            assert len(ali_server['requests']) == count
    finally:
        transcriber.close()
