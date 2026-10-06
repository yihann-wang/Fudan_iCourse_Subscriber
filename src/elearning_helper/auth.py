"""Fudan CAS login in a new, memory-only HTTP session.

No browser profiles, netrc, environment credentials, Keychain or cookie files.
The caller must provide credentials from an explicit, current user submission.
"""

import html
import json
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit, urlunsplit

from ..fudan_idp import AuthenticationFailed, FudanIDP, IDP_BASE
from .api import ConnectionFailure, LoginRequired, origin

AUTH_BODY_LIMIT = 1_000_000


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None  # Every redirect is checked before any further request.


class AuthResponse:
    def __init__(self, body):
        self.text = body.decode("utf-8-sig", "replace")

    def json(self):
        return json.loads(self.text.removeprefix("while(1);"))


class MemorySchoolSession:
    def __init__(self, base="https://elearning.fudan.edu.cn", *, timeout=25):
        self.base = base.rstrip("/")
        if self.base != "https://elearning.fudan.edu.cn":
            raise ConnectionFailure("账号密码登录仅支持已确认的复旦 eLearning 地址。")
        self.timeout = timeout
        self.cookies = CookieJar()
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(self.cookies))
        self.anonymous = urllib.request.build_opener(NoRedirect())
        self.authenticated = False

    def close(self):
        self.cookies.clear()
        self.authenticated = False

    def _validate_auth_url(self, url):
        p = urlsplit(url)
        if (p.username or p.password or p.scheme != "https" or p.port not in (None, 443)
                or origin(url) not in {origin(self.base), origin(IDP_BASE)}):
            raise ConnectionFailure("登录跳转离开已确认的学校身份服务，已停止。")

    def _open(self, url, *, method="GET", payload=None, headers=None, authenticated=True):
        p = urlsplit(url)
        # SPA fragments contain the IDP context, but must never be sent to HTTP.
        url = urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))
        request_headers = {"User-Agent": "Fudan-eLearning-Helper/0.1", "Accept-Encoding": "identity"}
        request_headers.update(headers or {})
        if authenticated and origin(url) == origin(self.base):
            # Canvas may require its CSRF header for session-authenticated API
            # requests. Only use cookies issued to this new program session.
            for cookie in self.cookies:
                if (cookie.name == "_csrf_token" and not cookie.is_expired()
                        and cookie.domain.lstrip(".") == urlsplit(self.base).hostname):
                    request_headers["X-CSRF-Token"] = unquote(cookie.value)
                    break
        request = urllib.request.Request(url, data=payload, headers=request_headers, method=method)
        try:
            return (self.opener if authenticated else self.anonymous).open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                return exc
            code = exc.code
            exc.close()
            if code == 401:
                raise LoginRequired("学校登录缺失或已失效，请重新亲自登录。") from None
            if code == 403:
                raise ConnectionFailure("学校拒绝访问（HTTP 403），未尝试绕过。") from None
            raise ConnectionFailure(f"学校请求失败（HTTP {code}）。") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ConnectionFailure("学校连接失败；未更改代理、证书或安全设置。") from None

    @staticmethod
    def _read(response):
        try:
            body = response.read(AUTH_BODY_LIMIT + 1)
            if len(body) > AUTH_BODY_LIMIT:
                raise ConnectionFailure("登录响应过大，已停止。")
            return body
        finally:
            response.close()

    def request(self, method, url, *, json=None, data=None, headers=None, timeout=None):
        self._validate_auth_url(url)
        if origin(url) != origin(IDP_BASE):
            raise ConnectionFailure("密码认证请求只能发送到复旦身份服务。")
        allowed = {
            ("POST", "/idp/authn/queryAuthMethods"),
            ("GET", "/idp/authn/getJsPublicKey"),
            ("POST", "/idp/authn/authExecute"),
            ("POST", "/idp/authCenter/authnEngine"),
        }
        if (method, urlsplit(url).path) not in allowed or urlsplit(url).query:
            raise ConnectionFailure("拒绝未确认的身份服务操作。")
        headers = dict(headers or {})
        payload = None
        if json is not None:
            import json as json_module
            payload = json_module.dumps(json).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif data is not None:
            payload = urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        response = self._open(url, method=method, payload=payload, headers=headers)
        if response.status != 200:
            response.close()
            # Never replay an auth POST across a redirect, even within the IDP.
            raise LoginRequired("身份服务要求重新认证或人工验证，已停止。")
        return AuthResponse(self._read(response))

    def _follow_login(self, start):
        url, context = start, None
        for _ in range(20):
            self._validate_auth_url(url)
            p = urlsplit(html.unescape(url))
            params = parse_qs(p.query)
            params.update(parse_qs(p.fragment.partition("?")[2]))
            if params.get("lck"):
                entity = params.get("entityId", [self.base])[0]
                if entity != self.base:
                    raise LoginRequired("身份服务返回了其他应用的认证上下文，已停止。")
                if origin(url) != origin(IDP_BASE):
                    raise LoginRequired("认证上下文来源不正确，已停止。")
                context = (params["lck"][0], entity)
            response = self._open(url)
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise LoginRequired("登录跳转缺少目标地址。")
                url = urljoin(url, location)
                continue
            self._read(response)
            return context
        raise LoginRequired("登录跳转次数过多，已停止。")

    def login(self, student_id, password):
        if not isinstance(student_id, str) or not isinstance(password, str) or not student_id.strip() or not password:
            raise LoginRequired("请在应用中亲自填写学号和统一身份认证密码。")
        try:
            context = self._follow_login(self.base + "/login")
            if context:
                ticket = FudanIDP(self.request).authenticate(student_id.strip(), password, *context)
                p = urlsplit(ticket)
                if (origin(ticket) != origin(self.base) or p.path != "/login/cas" or p.fragment
                        or p.username or p.password or not parse_qs(p.query).get("ticket")):
                    raise LoginRequired("CAS 回调不是 eLearning 登录地址，已停止。")
                self._follow_login(ticket)
            self.verify()
            self.authenticated = True
            # IDP state is not needed for subsequent Canvas reads.
            for cookie in list(self.cookies):
                if cookie.domain.lstrip(".") != "elearning.fudan.edu.cn":
                    self.cookies.clear(cookie.domain, cookie.path, cookie.name)
        except AuthenticationFailed as exc:
            self.close()
            raise LoginRequired(str(exc)) from None
        except BaseException:
            self.close()
            raise

    def verify(self):
        response = self._open(self.base + "/api/v1/users/self/profile")
        if response.status != 200 or "text/html" in response.headers.get("Content-Type", "").lower():
            response.close()
            raise LoginRequired("尚未取得有效的 eLearning 会话；不能将登录页当作成功。")
        try:
            profile = AuthResponse(self._read(response)).json()
            uid = profile.get("id") if isinstance(profile, dict) else None
            if isinstance(uid, bool) or not str(uid).isdecimal() or int(uid) <= 0:
                raise ValueError("invalid profile")
        except (ValueError, TypeError):
            raise LoginRequired("eLearning 登录成功校验未通过，未开始课程检查。") from None

    def open_data(self, url, *, download, validate):
        if not self.authenticated:
            raise LoginRequired("需要重新亲自登录 eLearning。")
        for _ in range(20):
            validate(url, download=download)
            p = urlsplit(url)
            if origin(url) == origin(self.base) and p.path.startswith(("/login", "/auth", "/saml", "/cas")):
                raise LoginRequired("eLearning 会话已失效，请重新亲自登录。")
            # Storage always uses a different, cookie-free opener. IDP/Canvas
            # cookies can never be forwarded there, including broad-domain ones.
            response = self._open(url, authenticated=origin(url) == origin(self.base))
            if response.status not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ConnectionFailure("下载或接口跳转缺少目标地址。")
            url = urljoin(url, location)
        raise ConnectionFailure("下载或接口跳转次数过多，已停止。")
