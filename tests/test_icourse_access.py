from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import config
from src.icourse import ICourseClient
from src.webvpn import WebVPNSession, get_vpn_url


def test_existing_school_session_does_not_repeat_cas(monkeypatch):
    vpn = WebVPNSession()
    vpn.session.get = Mock(side_effect=AssertionError("must reuse the session"))
    monkeypatch.setattr(ICourseClient, "check_alive", lambda self: True)
    assert vpn.authenticate_icourse()
    vpn.session.get.assert_not_called()


@pytest.mark.parametrize("sso_succeeded", [True, False])
def test_cas_without_challenge_requires_confirmed_login(monkeypatch, sso_succeeded):
    vpn = WebVPNSession()
    vpn.session.get = Mock(return_value=SimpleNamespace(
        status_code=200, headers={}, url=get_vpn_url(config.ICOURSE_BASE + "/"),
        text="<html>Course portal</html>",
    ))
    checks = iter([False, sso_succeeded])
    monkeypatch.setattr(ICourseClient, "check_alive", lambda self: next(checks))
    if sso_succeeded:
        assert vpn.authenticate_icourse()
    else:
        with pytest.raises(RuntimeError, match="Failed to extract lck"):
            vpn.authenticate_icourse()


@pytest.mark.parametrize("already_proxied", [False, True])
def test_replay_range_request_keeps_referrer_and_does_not_double_proxy(already_proxied):
    vpn = SimpleNamespace(get=Mock(), get_raw=Mock(), session=SimpleNamespace(cookies=[]))
    client = ICourseClient(vpn)
    url = config.ICOURSE_BASE + "/video.mp4?t=test"
    if already_proxied:
        url = get_vpn_url(url)
    response = client.get_video_response(url, headers={"Range": "bytes=0-0"}, timeout=10)
    getter = vpn.get_raw if already_proxied else vpn.get
    getter.assert_called_once_with(
        url, headers={"Referer": get_vpn_url(config.ICOURSE_BASE + "/"),
                      "Range": "bytes=0-0"}, stream=True, timeout=10,
    )
    assert response is getter.return_value
    (vpn.get if already_proxied else vpn.get_raw).assert_not_called()
    stream_url, headers = client.get_stream_params(url)
    assert stream_url == (url if already_proxied else get_vpn_url(url))
    assert f"Referer: {get_vpn_url(config.ICOURSE_BASE + '/')}\r\n" in headers


@pytest.mark.parametrize('method', ['get_sub_info', 'get_sub_detail'])
def test_scheduled_replay_is_distinct_from_permission_errors(method):
    from src.icourse import ReplayNotAvailableError
    response = Mock()
    response.json.return_value = dict(code=400, msg='视频未到开放时间')
    client = ICourseClient(SimpleNamespace(get=Mock(return_value=response)))
    with pytest.raises(ReplayNotAvailableError):
        getattr(client, method)('12345', '123456')
    response.json.return_value = dict(code=403, msg='没有访问权限')
    with pytest.raises(RuntimeError) as error:
        getattr(client, method)('12345', '123456')
    assert not isinstance(error.value, ReplayNotAvailableError)


def test_scheduled_replay_becomes_pending_without_download_retry(tmp_path, monkeypatch):
    import queue
    from src.icourse import ReplayNotAvailableError
    from src.pipeline import _Counters, _download_stage
    from src.pipeline_state import PipelineState
    state = PipelineState(tmp_path / 'state.db')
    incoming, outgoing = state.queue('download'), queue.Queue()
    incoming.put(dict(course_id='12345', sub_id='123456', target_video_path=tmp_path / 'lecture.mp4'))
    incoming.put(None)
    client = SimpleNamespace(get_video_url=Mock(side_effect=ReplayNotAvailableError('回放未到开放时间')),
                             check_alive=Mock(side_effect=AssertionError('Must not relogin')))
    monkeypatch.setattr('src.pipeline._stage_event', lambda *_: None)
    counters = _Counters()
    _download_stage(incoming, outgoing, client, 0, counters)
    assert counters.pending == 1 and counters.failed == 0
    client.get_video_url.assert_called_once()
    assert outgoing.get_nowait() is None and outgoing.empty()
    assert state.connection.execute("SELECT status FROM pipeline_jobs WHERE sub_id='123456'").fetchone()[0] == 'pending'
    state.close()
