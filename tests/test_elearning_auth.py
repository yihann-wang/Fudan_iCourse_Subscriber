"""Login chain fixtures contain invented credentials and never contact school."""

import io
import json
from http.cookiejar import Cookie
from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import Mock

import pytest
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

from src.elearning_helper.api import CanvasClient, ConnectionFailure, LoginRequired
from src.elearning_helper.auth import MemorySchoolSession
from src.fudan_idp import FudanIDP, IDP_BASE
from src.webvpn import WebVPNSession, get_vpn_url

BASE = "https://elearning.fudan.edu.cn"
STORAGE = "https://canvas-production.s3.fudan.edu.cn"
STUDENT, PASSWORD = "TEST_STUDENT", "TEST_PASSWORD_NOT_REAL"


class Response(io.BytesIO):
    def __init__(self, body=b"", *, status=200, headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        if isinstance(body, str):
            body = body.encode()
        super().__init__(body)
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append(request)
        return next(self.responses)


@pytest.fixture(scope="module")
def rsa():
    return RSA.generate(1024)  # Test only; server-provided public keys are used in production.


def redirect(url):
    return Response(status=302, headers={"Location": url})


def flow(rsa, *, auth=None, ticket=None, profile=None):
    key = "".join(rsa.public_key().export_key().decode().splitlines()[1:-1])
    return [
        redirect(BASE + "/login/cas"),
        redirect(IDP_BASE + "/idp/authCenter/authenticate?service=" + quote(BASE + "/login/cas", safe="")),
        redirect(IDP_BASE + "/ac/#/index?lck=TEST_CONTEXT&entityId=" + quote(BASE, safe="") + "&theme=test"),
        Response("<html>public IDP application</html>"),
        Response({"data": [{"moduleCode": "userAndPwd", "authChainCode": "TEST_CHAIN"}], "requestType": "chain_type"}),
        Response({"data": key}),
        Response(auth if auth is not None else {"code": "200", "loginToken": "TEST_LOGIN_TOKEN"}),
        Response('var locationValue="' + (ticket or BASE + "/login/cas?ticket=TEST_SERVICE_TICKET") + '";'),
        redirect(BASE + "/"),
        Response("<html>Canvas portal</html>"),
        profile if profile is not None else Response({"id": 123}),
    ]


def test_cas_chain_encrypts_password_and_verifies_actual_canvas_session(rsa, capsys):
    import base64
    session = MemorySchoolSession()
    opener = Opener(flow(rsa) + [Response([{"id": 9, "name": "fixture"}])])
    session.opener = opener
    session.login(STUDENT, PASSWORD)
    assert session.authenticated
    assert len(opener.calls) == 11
    assert all("#" not in request.full_url for request in opener.calls)
    submitted = next(r for r in opener.calls if r.full_url.endswith("/authExecute"))
    payload = json.loads(submitted.data)
    assert payload["entityId"] == BASE and payload["lck"] == "TEST_CONTEXT"
    assert payload["authPara"]["loginName"] == STUDENT
    encrypted = base64.b64decode(payload["authPara"]["password"])
    assert PKCS1_v1_5.new(rsa).decrypt(encrypted, None).decode() == PASSWORD
    assert PASSWORD.encode() not in submitted.data
    assert opener.calls[-1].full_url == BASE + "/api/v1/users/self/profile"
    client = CanvasClient(BASE, session=session)
    assert client.files("114667") == [{"id": 9, "name": "fixture"}]
    assert not opener.calls[-1].has_header("Authorization")
    client.close()
    assert not session.authenticated and len(session.cookies) == 0
    assert not capsys.readouterr().out


@pytest.mark.parametrize("auth", [
    {"code": 401, "message": PASSWORD},
    {"code": 200, "needCaptcha": True, "loginToken": "TEST_LOGIN_TOKEN"},
    {"code": 200, "needMfa": True},
    {"code": 200, "secondAuth": {"challenge": PASSWORD}},
    {"code": 200},
])
def test_bad_password_or_interaction_stops_without_retry_or_secret_message(rsa, auth):
    session = MemorySchoolSession()
    opener = Opener(flow(rsa, auth=auth))
    session.opener = opener
    with pytest.raises(LoginRequired) as error:
        session.login(STUDENT, PASSWORD)
    assert PASSWORD not in str(error.value)
    assert len(opener.calls) == 7
    assert not session.authenticated and not list(session.cookies)


@pytest.mark.parametrize("ticket", [
    "https://unapproved.invalid/login/cas?ticket=TEST",
    "https://icourse.fudan.edu.cn/casapi/index.php?ticket=TEST",
    BASE + "/not-cas?ticket=TEST",
    "http://elearning.fudan.edu.cn/login/cas?ticket=TEST",
])
def test_only_elearning_cas_callback_can_receive_ticket(rsa, ticket):
    session = MemorySchoolSession()
    opener = Opener(flow(rsa, ticket=ticket))
    session.opener = opener
    with pytest.raises(LoginRequired):
        session.login(STUDENT, PASSWORD)
    assert len(opener.calls) == 8


@pytest.mark.parametrize("profile", [
    lambda: Response("<html>login</html>", headers={"Content-Type": "text/html"}),
    lambda: Response({"id": False}),
    lambda: Response({"id": 0}),
    lambda: Response([]),
    lambda: redirect(BASE + "/login"),
])
def test_portal_or_http_200_alone_is_never_login_success(rsa, profile):
    session = MemorySchoolSession()
    session.opener = Opener(flow(rsa, profile=profile()))
    with pytest.raises(LoginRequired):
        session.login(STUDENT, PASSWORD)
    assert not session.authenticated


def test_login_redirect_cannot_send_anything_to_unknown_host():
    session = MemorySchoolSession()
    opener = Opener([redirect("https://unknown.invalid/collect")])
    session.opener = opener
    with pytest.raises(ConnectionFailure):
        session.login(STUDENT, PASSWORD)
    assert len(opener.calls) == 1


def test_idp_context_for_other_service_is_rejected():
    session = MemorySchoolSession()
    opener = Opener([redirect(IDP_BASE + "/ac/#/index?lck=TEST&entityId=https%3A%2F%2Ficourse.fudan.edu.cn")])
    session.opener = opener
    with pytest.raises(LoginRequired, match="其他应用"):
        session.login(STUDENT, PASSWORD)
    assert len(opener.calls) == 1


def test_auth_post_redirect_is_not_replayed():
    session = MemorySchoolSession()
    opener = Opener([redirect(IDP_BASE + "/unexpected")])
    session.opener = opener
    with pytest.raises(LoginRequired):
        session.request("POST", IDP_BASE + "/idp/authn/authExecute", json={"fixture": "data"})
    assert len(opener.calls) == 1


def test_storage_download_uses_cookie_free_transport_and_rechecks_redirects():
    session = MemorySchoolSession()
    session.authenticated = True
    session.cookies.set_cookie(Cookie(0, "broad_fixture", "TEST_COOKIE", None, False,
        ".fudan.edu.cn", True, True, "/", True, True, None, True, None, None, {}))
    session.cookies.set_cookie(Cookie(0, "_csrf_token", "TEST%2FCSRF", None, False,
        "elearning.fudan.edu.cn", False, False, "/", True, True, None, True, None, None, {}))
    school = Opener([redirect(STORAGE + "/document.pdf")])
    storage = Opener([Response(b"%PDF-1.4\nfixture\n%%EOF\n")])
    session.opener, session.anonymous = school, storage
    client = CanvasClient(BASE, download_hosts=("canvas-production.s3.fudan.edu.cn",), session=session)
    with client.open(BASE + "/files/123/download", download=True) as response:
        assert response.read().startswith(b"%PDF")
    assert len(school.calls) == len(storage.calls) == 1
    assert school.calls[0].get_header("X-csrf-token") == "TEST/CSRF"
    assert not storage.calls[0].has_header("X-csrf-token")
    assert not storage.calls[0].has_header("Cookie") and not storage.calls[0].has_header("Authorization")
    storage.responses = iter([redirect("https://unapproved.invalid/file")])
    with pytest.raises(ConnectionFailure):
        client.open(STORAGE + "/document.pdf", download=True)
    assert len(storage.calls) == 2
    client.close()
    assert not list(session.cookies)


def test_api_redirect_to_login_stops_without_resubmitting_credentials():
    session = MemorySchoolSession()
    session.authenticated = True
    opener = Opener([redirect(BASE + "/login/cas")])
    session.opener = opener
    client = CanvasClient(BASE, session=session)
    with pytest.raises(LoginRequired):
        client.files("114667")
    assert len(opener.calls) == 1


def test_shared_idp_keeps_webvpn_routing_and_request_payload(rsa):
    key = "".join(rsa.public_key().export_key().decode().splitlines()[1:-1])
    responses = iter([
        SimpleNamespace(json=lambda: {"data": [{"moduleCode": "userAndPwd", "authChainCode": "CHAIN"}]}),
        SimpleNamespace(json=lambda: {"data": key}),
        SimpleNamespace(json=lambda: {"code": 200, "loginToken": "TOKEN"}),
        SimpleNamespace(text='var locationValue="https://icourse.fudan.edu.cn/casapi/index.php?ticket=TEST&amp;r=auth/login";'),
    ])
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return next(responses)
    idp = FudanIDP(request, mapper=get_vpn_url, origin="https://webvpn.fudan.edu.cn")
    ticket = idp.authenticate(STUDENT, PASSWORD, "LCK", "https://icourse.fudan.edu.cn")
    assert "&r=auth/login" in ticket
    assert all(url.startswith("https://webvpn.fudan.edu.cn/") for _, url, _ in calls)
    assert calls[2][2]["json"]["entityId"] == "https://icourse.fudan.edu.cn"
    assert calls[3][2]["data"] == {"loginToken": "TOKEN"}
    assert calls[1][0] == "GET"
    assert calls[0][2]["headers"]["Origin"] == "https://webvpn.fudan.edu.cn"


def test_webvpn_login_still_uses_its_own_service_and_success_flow(monkeypatch):
    vpn = WebVPNSession()
    expected = "https://webvpn.fudan.edu.cn/login?cas_login=true&ticket=TEST"
    monkeypatch.setattr(vpn, "_get_auth_context", lambda: ("LCK", "https://webvpn.fudan.edu.cn"))
    monkeypatch.setattr(vpn, "_query_auth_methods", lambda *a: ("CHAIN", "chain_type"))
    monkeypatch.setattr(vpn, "_get_public_key", lambda: "KEY")
    monkeypatch.setattr(vpn, "_encrypt_password", lambda *a: "ENCRYPTED")
    execute = Mock(return_value="TOKEN")
    monkeypatch.setattr(vpn, "_auth_execute", execute)
    monkeypatch.setattr(vpn, "_get_cas_ticket", lambda *a: expected)
    establish = Mock()
    monkeypatch.setattr(vpn, "_establish_session", establish)
    assert vpn.login(STUDENT, PASSWORD) and vpn.logged_in
    assert execute.call_args.args[3] == "https://webvpn.fudan.edu.cn"
    establish.assert_called_once_with(expected)


@pytest.mark.parametrize("dry_run", [True, False])
def test_private_login_pipe_through_real_sync_core(rsa, monkeypatch, tmp_path, capsys, dry_run):
    from src.elearning_helper import __main__ as worker, auth
    pdf = b"%PDF-1.4\nlocal authentication integration fixture\n%%EOF\n"
    session = MemorySchoolSession()
    file = {"id": 1, "folder_id": 1, "display_name": "fixture.pdf", "size": len(pdf),
            "content-type": "application/pdf", "modified_at": "2026-10-04T00:00:00Z",
            "url": BASE + "/files/1/download"}
    assignment = {"id": 7, "name": "fixture assignment", "due_at": "2026-10-11T15:59:00Z",
                  "submission": {"workflow_state": "unsubmitted"}}
    responses = flow(rsa) + [Response([assignment]), Response([file])]
    if not dry_run:
        responses.append(Response(pdf, headers={"Content-Type": "application/pdf", "Content-Length": str(len(pdf))}))
    opener = Opener(responses)
    session.opener = opener
    monkeypatch.setattr(auth, "MemorySchoolSession", lambda _: session)
    monkeypatch.setattr(worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps({
        "student_id": STUDENT, "password": PASSWORD}).encode())))
    old_get = worker.os.environ.get
    def no_ambient_secrets(key, default=None):
        if key in {"FUDAN_ELEARNING_TOKEN", "StuId", "UISPsw"}:
            pytest.fail("GUI login must not read ambient credentials")
        return old_get(key, default)
    monkeypatch.setattr(worker.os.environ, "get", no_ambient_secrets)
    config = {"base_url": BASE, "root": str(tmp_path / "courses"), "state_dir": str(tmp_path / "state"),
              "courses": [{"id": "114614", "name": "fixture course", "directory": "algorithm", "include_folders": ["课件"]}]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    args = ["--config", str(path), "--login-stdin", "sync"] + (["--dry-run"] if dry_run else [])
    assert worker.main(args) == 0
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err and STUDENT not in output.out + output.err
    assert "fixture assignment" in output.out and "未提交" in output.out
    assert not session.authenticated and not list(session.cookies)
    if dry_run:
        assert not (tmp_path / "courses").exists() and not (tmp_path / "state").exists()
    else:
        assert (tmp_path / "courses/algorithm/fixture.pdf").read_bytes() == pdf
        assert (tmp_path / "state/index.sqlite3").is_file()
    for item in tmp_path.rglob("*"):
        if item.is_file():
            assert PASSWORD.encode() not in item.read_bytes()


def test_failed_gui_login_never_creates_state_or_echoes_credentials(rsa, monkeypatch, tmp_path, capsys):
    from src.elearning_helper import __main__ as worker, auth
    session = MemorySchoolSession()
    session.opener = Opener(flow(rsa, auth={"code": 401, "message": PASSWORD}))
    monkeypatch.setattr(auth, "MemorySchoolSession", lambda _: session)
    monkeypatch.setattr(worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps({
        "student_id": STUDENT, "password": PASSWORD}).encode())))
    config = {"base_url": BASE, "root": str(tmp_path / "courses"), "state_dir": str(tmp_path / "state"),
              "courses": [{"id": "114614", "name": "fixture", "directory": "algorithm"}]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    assert worker.main(["--config", str(path), "--login-stdin", "sync"]) == 3
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err and STUDENT not in output.out + output.err
    assert not (tmp_path / "state").exists() and not (tmp_path / "courses").exists()
    assert not session.authenticated
