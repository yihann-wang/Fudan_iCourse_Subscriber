"""Read-only Canvas client. Never reads browser profiles or persisted credentials."""

import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlencode, urljoin, urlsplit


class ConnectionFailure(RuntimeError):
    pass


class LoginRequired(ConnectionFailure):
    pass


def origin(url):
    value = urlsplit(url)
    return value.scheme, value.hostname, value.port or (443 if value.scheme == "https" else 80)


def pagination_next(headers, current_url):
    """Follow Canvas's opaque Link URL; never infer completion from page length."""
    values = headers.get_all("Link", []) if hasattr(headers, "get_all") else [headers.get("Link", "")]
    raw = ",".join(value for value in values if value).strip()
    if not raw:
        return None
    next_links = []
    for part in re.split(r",\s*(?=<)", raw):
        match = re.fullmatch(r"\s*<([^>]+)>\s*(.*)", part, re.DOTALL)
        if not match:
            raise ConnectionFailure("分页 Link 格式异常，未接受不完整清单")
        target, parameters = match.groups()
        relation = re.search(r'(?:^|;)\s*rel\s*=\s*(?:"([^"]*)"|([^;\s,]+))', parameters, re.IGNORECASE)
        if not relation:
            raise ConnectionFailure("分页 Link 缺少关系，未接受不完整清单")
        if "next" in (relation.group(1) or relation.group(2)).lower().split():
            next_links.append(urljoin(current_url, target))
    if len(set(next_links)) > 1:
        raise ConnectionFailure("分页出现多个下一页，未接受不完整清单")
    return next_links[0] if next_links else None


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, client, download):
        self.client, self.download = client, download

    def redirect_request(self, request, response, code, message, headers, newurl):
        newurl = urljoin(request.full_url, newurl)
        self.client.validate_url(newurl, download=self.download)
        if re.search(r"/(login|auth|saml|cas)(/|\?|$)", newurl):
            raise LoginRequired("学校要求重新登录；未跟随登录表单")
        redirected = super().redirect_request(request, response, code, message, headers, newurl)
        # Never forward the Canvas credential to storage servers or other origins.
        if redirected is not None and origin(newurl) != origin(self.client.base):
            redirected.remove_header("Authorization")
            redirected.remove_header("Cookie")
        return redirected


class CanvasClient:
    def __init__(self, base, token=None, download_hosts=(), *, session=None, timeout=25, allow_loopback=False):
        self.base = base.rstrip("/")
        self.token = token
        self.session = session
        self.download_hosts = set(download_hosts)
        self.timeout = timeout
        self.allow_loopback = allow_loopback  # Tests only; not exposed in user config/CLI.
        self.page_counts = {}
        if session is None and (not token or "\r" in token or "\n" in token):
            raise LoginRequired("未配置可用认证；程序不会继承浏览器登录或读取现有凭据")
        if session is not None and (token or not session.authenticated or session.base != self.base):
            raise LoginRequired("eLearning 内存会话未通过登录校验。")
        self.validate_url(self.base)

    def validate_url(self, url, *, download=False):
        parsed = urlsplit(url)
        if parsed.username or parsed.password or parsed.fragment:
            raise ConnectionFailure("拒绝带用户信息或片段的地址")
        loopback = self.allow_loopback and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        if not loopback and (parsed.scheme != "https" or parsed.port not in (None, 443)):
            raise ConnectionFailure("仅允许 HTTPS，未降低证书验证要求")
        same = origin(url) == origin(self.base)
        permitted_storage = download and parsed.hostname in self.download_hosts
        if not same and not permitted_storage:
            raise ConnectionFailure("拒绝访问未获配置许可的主机；未发送认证信息")

    def open(self, url, *, download=False):
        self.validate_url(url, download=download)
        if self.session is not None:
            return self.session.open_data(url, download=download, validate=self.validate_url)
        headers = {"User-Agent": "Fudan-eLearning-Helper/0.1", "Accept-Encoding": "identity"}
        if origin(url) == origin(self.base):
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(url, headers=headers, method="GET")
        # No proxy, TLS, browser-security or system-setting modifications.
        opener = urllib.request.build_opener(SafeRedirect(self, download))
        try:
            return opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 401:
                raise LoginRequired("认证缺失、已失效或被撤销（HTTP 401），需用户重新授权") from None
            if code == 403:
                raise ConnectionFailure("学校拒绝访问（HTTP 403），不等同于登录过期") from None
            raise ConnectionFailure(f"学校请求失败（HTTP {code}），没有把失败记作检查成功") from None
        except urllib.error.URLError:
            raise ConnectionFailure("网络、代理或证书连接失败；未绕过限制，未记录含签名的网址") from None
        except (TimeoutError, OSError):
            raise ConnectionFailure("连接中断或超时，稍后可手动重试") from None

    def close(self):
        self.token = None
        if self.session is not None:
            self.session.close()

    def list(self, path, **params):
        self.page_counts.pop(path, None)
        url = self.base + path + "?" + urlencode({"per_page": 100, **params})
        seen = set()
        result = []
        while url:
            if url in seen or len(seen) >= 100:
                raise ConnectionFailure("分页循环或超过 100 页，未接受不完整清单")
            seen.add(url)
            with self.open(url) as response:
                content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
                if content_type == "text/html":
                    raise LoginRequired("接口返回 HTML 登录/拦截页面，未作为空清单处理")
                raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise ConnectionFailure("API 单页超过安全读取上限")
                try:
                    text = raw.decode("utf-8-sig").removeprefix("while(1);")
                    rows = json.loads(text)
                except (UnicodeError, ValueError):
                    raise ConnectionFailure("接口未返回有效 JSON 清单") from None
                if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
                    raise ConnectionFailure("接口响应结构不符，未接受为课程清单")
                result.extend(rows)
                url = pagination_next(response.headers, url)
                if url:
                    self.validate_url(url)
        self.page_counts[path] = len(seen)
        return result

    def files(self, course_id):
        return self.list(f"/api/v1/courses/{course_id}/files")

    def folders(self, course_id):
        return self.list(f"/api/v1/courses/{course_id}/folders")

    def assignments(self, course_id):
        # Canvas applies dates for the current student, including individual overrides.
        return self.list(f"/api/v1/courses/{course_id}/assignments",
                         **{"include[]": "submission", "override_assignment_dates": "true"})
