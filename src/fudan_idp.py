"""Shared Fudan IDP protocol. No configuration, credential storage or browser access."""

import base64
import html
import re

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

IDP_BASE = "https://id.fudan.edu.cn"


class AuthenticationFailed(RuntimeError):
    pass


class InteractionRequired(AuthenticationFailed):
    pass


def encrypt_password(password, public_key):
    pem = "-----BEGIN PUBLIC KEY-----\n" + public_key + "\n-----END PUBLIC KEY-----"
    cipher = PKCS1_v1_5.new(RSA.import_key(pem))
    return base64.b64encode(cipher.encrypt(password.encode("utf-8"))).decode("ascii")


def extract_ticket_url(body):
    match = re.search(r'locationValue\s*=\s*"([^\"]*ticket=[^\"]*)"', body)
    if not match:
        match = re.search(r'(https?://[^\s"\'<>]*ticket=[^\s"\'<>]*)', body)
    if not match:
        raise AuthenticationFailed("身份服务未返回 CAS 回调；可能需要人工验证，本次已停止。")
    return html.unescape(match.group(1))


class FudanIDP:
    """Reuse the existing userAndPwd/RSA/CAS flow with an injected transport.

    ``request`` returns an object with .json() and .text. WebVPN supplies its
    requests.Session; eLearning supplies a bounded, origin-restricted transport.
    """

    def __init__(self, request, *, mapper=lambda url: url, origin=IDP_BASE):
        self.request = request
        self.mapper = mapper
        self.headers = {"Referer": mapper(IDP_BASE) + "/ac/", "Origin": origin}

    def _json(self, method, path, **kwargs):
        response = self.request(method, self.mapper(IDP_BASE + path),
                                headers=self.headers, timeout=30, **kwargs)
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise AuthenticationFailed("身份服务返回异常，未继续提交或重试登录。") from None
        if not isinstance(data, dict):
            raise AuthenticationFailed("身份服务响应结构异常，未继续登录。")
        return data

    def query_methods(self, lck, entity_id):
        data = self._json("POST", "/idp/authn/queryAuthMethods",
                          json={"lck": lck, "entityId": entity_id})
        methods = data.get("data", [])
        if not isinstance(methods, list):
            raise AuthenticationFailed("未取得可用的认证方式，请重新开始登录。")
        for method in methods:
            if isinstance(method, dict) and method.get("moduleCode") == "userAndPwd":
                chain = method.get("authChainCode")
                if chain:
                    return chain, data.get("requestType", "chain_type")
        raise InteractionRequired("学校要求其他认证方式或二次验证；已停止，请由用户处理。")

    def public_key(self):
        data = self._json("GET", "/idp/authn/getJsPublicKey")
        key = data.get("data")
        if not isinstance(key, str) or not key:
            raise AuthenticationFailed("未取得身份服务的密码加密公钥。")
        return key

    def execute(self, student_id, encrypted_password, lck, entity_id, chain, request_type):
        data = self._json("POST", "/idp/authn/authExecute", json={
            "authModuleCode": "userAndPwd", "authChainCode": chain,
            "entityId": entity_id, "requestType": request_type, "lck": lck,
            "authPara": {"loginName": student_id, "password": encrypted_password, "verifyCode": ""},
        })
        # Never print server messages or challenge objects: they may contain secrets.
        if any(data.get(key) for key in ("needVerifyCode", "needCaptcha", "needMfa", "secondAuth")):
            raise InteractionRequired("学校要求验证码或二次验证；已停止，请由用户处理。")
        if str(data.get("code")) != "200":
            raise AuthenticationFailed("学校未通过账号密码认证。请核对输入；若学校要求验证码或二次验证，需由用户处理。")
        token = data.get("loginToken")
        if not isinstance(token, str) or not token:
            raise InteractionRequired("身份服务尚未完成认证；可能需要二次验证，已停止。")
        return token

    def ticket(self, login_token):
        response = self.request("POST", self.mapper(IDP_BASE + "/idp/authCenter/authnEngine"),
                                data={"loginToken": login_token}, headers=self.headers, timeout=30)
        return extract_ticket_url(response.text)

    def authenticate(self, student_id, password, lck, entity_id):
        chain, request_type = self.query_methods(lck, entity_id)
        encrypted = encrypt_password(password, self.public_key())
        token = self.execute(student_id, encrypted, lck, entity_id, chain, request_type)
        return self.ticket(token)
