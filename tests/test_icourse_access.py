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
