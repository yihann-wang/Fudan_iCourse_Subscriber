"""
WebVPN URL encoding and authentication for Fudan University.

Handles:
- AES-128-CFB URL encoding/decoding for WebVPN proxy URLs
- Full 7-step IDP authentication flow against id.fudan.edu.cn
"""

import re
from binascii import hexlify, unhexlify
from urllib.parse import urlparse, quote, urljoin

import requests
from Crypto.Cipher import AES

from . import config
from .fudan_idp import FudanIDP, encrypt_password


def encrypt_host(hostname: str) -> str:
    """Encrypt hostname using AES-128-CFB for WebVPN URL encoding.

    Returns the hex-encoded ciphertext of the hostname.
    """
    key = config.WEBVPN_AES_KEY
    iv = config.WEBVPN_AES_IV
    cipher = AES.new(key, AES.MODE_CFB, iv, segment_size=128)
    plaintext = hostname.encode("utf-8")
    encrypted = cipher.encrypt(plaintext)
    return hexlify(encrypted).decode("ascii")


def decrypt_host(ciphertext_hex: str) -> str:
    """Decrypt a WebVPN-encoded hostname."""
    key = config.WEBVPN_AES_KEY
    iv = config.WEBVPN_AES_IV
    cipher = AES.new(key, AES.MODE_CFB, iv, segment_size=128)
    decrypted = cipher.decrypt(unhexlify(ciphertext_hex))
    return decrypted.decode("utf-8")


def get_vpn_url(url: str) -> str:
    """Convert a regular URL to its WebVPN proxy URL.

    Example:
        https://icourse.fudan.edu.cn/courseapi/v3/...
        ->
        https://webvpn.fudan.edu.cn/https/77726476706e69737468656265737421f9f44e.../courseapi/v3/...
    """
    parsed = urlparse(url)
    protocol = parsed.scheme
    hostname = parsed.hostname
    port = parsed.port
    path = parsed.path
    if parsed.query:
        path += "?" + parsed.query
    if parsed.fragment:
        path += "#" + parsed.fragment

    # Remove leading slash from path for concatenation
    path = path.lstrip("/")

    encrypted = encrypt_host(hostname)
    iv_hex = hexlify(config.WEBVPN_AES_IV).decode("ascii")

    # Include non-standard port
    port_suffix = ""
    if port and not (
        (protocol == "http" and port == 80)
        or (protocol == "https" and port == 443)
    ):
        port_suffix = f"-{port}"

    vpn_url = f"{config.WEBVPN_BASE}/{protocol}{port_suffix}/{iv_hex}{encrypted}"
    if path:
        vpn_url += f"/{path}"
    return vpn_url


def get_ordinary_url(vpn_url: str) -> str:
    """Convert a WebVPN URL back to the original URL."""
    parsed = urlparse(vpn_url)
    path_parts = parsed.path.strip("/").split("/", 2)
    if len(path_parts) < 2:
        raise ValueError(f"Invalid WebVPN URL: {vpn_url}")

    protocol_part = path_parts[0]  # e.g. "https" or "https-8080"
    encoded_host = path_parts[1]  # IV + ciphertext
    rest = path_parts[2] if len(path_parts) > 2 else ""

    # Parse protocol and optional port
    if "-" in protocol_part:
        protocol, port_str = protocol_part.rsplit("-", 1)
        port = f":{port_str}"
    else:
        protocol = protocol_part
        port = ""

    # Strip the 32-char IV hex prefix
    iv_hex_len = 32
    ciphertext_hex = encoded_host[iv_hex_len:]
    hostname = decrypt_host(ciphertext_hex)

    original = f"{protocol}://{hostname}{port}"
    if rest:
        original += f"/{rest}"
    if parsed.query:
        original += f"?{parsed.query}"
    return original


class WebVPNSession:
    """Manages a WebVPN session with full IDP authentication."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        self.logged_in = False

    def login(self, student_id: str = None, password: str = None) -> bool:
        """Execute the full 7-step IDP authentication flow.

        Returns True on success, raises on failure.
        """
        student_id = student_id or config.STUDENT_ID
        password = password or config.PASSWORD

        if not student_id or not password:
            raise ValueError(
                "Student ID and password are required. "
                "Set STUID and UISPsw environment variables."
            )

        print("[1/7] Getting authentication context...")
        lck, entity_id = self._get_auth_context()

        print("[2/7] Querying authentication methods...")
        auth_chain_code, request_type = self._query_auth_methods(lck, entity_id)

        print("[3/7] Getting RSA public key...")
        pub_key_pem = self._get_public_key()

        print("[4/7] Encrypting password...")
        encrypted_password = self._encrypt_password(password, pub_key_pem)

        print("[5/7] Executing authentication...")
        login_token = self._auth_execute(
            student_id,
            encrypted_password,
            lck,
            entity_id,
            auth_chain_code,
            request_type,
        )

        print("[6/7] Getting CAS ticket...")
        ticket_url = self._get_cas_ticket(login_token)

        print("[7/7] Establishing WebVPN session...")
        self._establish_session(ticket_url)

        self.logged_in = True
        print("[*] WebVPN login successful!")
        return True

    def authenticate_icourse(
        self, student_id: str = None, password: str = None
    ) -> bool:
        """Authenticate to iCourse via CAS/IDP through WebVPN.

        Mimics the browser flow:
        1. Access casapi login URL (like clicking "校内用户登录")
        2. Follow redirect to IDP authenticate (casapi generates correct
           service URL with forward param and r=auth/login)
        3. IDP auth steps through WebVPN
        4. Follow ticket back to iCourse through WebVPN
        """
        from .icourse import ICourseClient

        client = ICourseClient(self)
        if client.check_alive():
            print("[*] iCourse session is already authenticated.")
            return True

        student_id = student_id or config.STUDENT_ID
        password = password or config.PASSWORD

        print("[*] Starting iCourse CAS authentication through WebVPN...")

        # Step 1: Initiate CAS login via casapi
        # This is equivalent to clicking "校内用户登录" in the browser.
        # casapi will 302-redirect to IDP with the correct service URL:
        #   service=https://icourse.fudan.edu.cn/casapi/index.php
        #          ?forward=https%3A%2F%2Ficourse.fudan.edu.cn%2F&r=auth/login
        print("[1/7] Initiating CAS login via casapi...")
        casapi_url = (
            f"{config.ICOURSE_BASE}/casapi/index.php"
            f"?r=auth/login&school_login=1"
            f"&tenant_code={config.TENANT_CODE}"
            f"&forward={quote(config.ICOURSE_BASE + '/', safe='')}"
        )
        vpn_url = get_vpn_url(casapi_url)

        # Follow redirect chain to reach IDP login page and extract lck
        resp = self.session.get(vpn_url, allow_redirects=False, timeout=30)
        lck = None
        for _ in range(15):
            location = resp.headers.get("Location", "")
            if resp.status_code not in (301, 302, 303, 307) or not location:
                break
            lck_match = re.search(r'lck=([^&#"]+)', location)
            if lck_match:
                lck = lck_match.group(1)
                break
            if not location.startswith("http"):
                location = urljoin(resp.url, location)
            resp = self.session.get(
                location, allow_redirects=False, timeout=30
            )

        if not lck:
            # Check final response URL and body
            for source in [resp.url, resp.text[:5000]]:
                m = re.search(r'lck=([^&#"]+)', source)
                if m:
                    lck = m.group(1)
                    break
        if not lck:
            # CAS can finish an existing SSO session without presenting a new
            # IDP challenge. Confirm login through the API, not the HTML page.
            if client.check_alive():
                print("[*] iCourse SSO authentication successful.")
                return True
            raise RuntimeError(
                f"Failed to extract lck from CAS redirect chain (status={resp.status_code})"
            )

        entity_id = config.ICOURSE_BASE
        print("    lck: OK")

        # Both applications use the same Fudan IDP protocol; only routing and
        # the CAS service differ. Keep iCourse's existing callback and check.
        idp = FudanIDP(
            lambda method, url, **kwargs: getattr(self.session, method.lower())(url, **kwargs),
            mapper=get_vpn_url, origin=config.WEBVPN_BASE,
        )
        ticket_url = idp.authenticate(student_id, password, lck, entity_id)

        # Step 7: Follow ticket to iCourse (through WebVPN)
        print("[7/7] Following ticket to iCourse (via WebVPN)...")
        if not ticket_url.startswith(config.WEBVPN_BASE):
            ticket_url = get_vpn_url(ticket_url)

        resp = self.session.get(
            ticket_url, allow_redirects=True, timeout=90
        )
        print(f"    Status: {resp.status_code}")

        # Verify by making a test API call
        test_url = get_vpn_url(
            f"{config.ICOURSE_BASE}/userapi/v1/infosimple"
        )
        resp = self.session.get(test_url, timeout=30)
        if resp.status_code == 200:
            try:
                user_data = resp.json()
                if user_data.get("code") in (0, 200):
                    print("    Verified: login OK")
                    print("[*] iCourse authentication successful!")
                    return True
            except Exception:
                pass

        raise RuntimeError("iCourse 登录未通过验证，请检查账号或稍后重试。")

    def get(self, url: str, **kwargs) -> requests.Response:
        """GET request through WebVPN. Converts URL automatically."""
        vpn_url = get_vpn_url(url)
        kwargs.setdefault("timeout", 60)
        return self.session.get(vpn_url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        """POST request through WebVPN. Converts URL automatically."""
        vpn_url = get_vpn_url(url)
        kwargs.setdefault("timeout", 60)
        return self.session.post(vpn_url, **kwargs)

    def get_raw(self, url: str, **kwargs) -> requests.Response:
        """GET request without URL conversion (for already-converted URLs)."""
        kwargs.setdefault("timeout", 30)
        return self.session.get(url, **kwargs)

    def post_raw(self, url: str, **kwargs) -> requests.Response:
        """POST request without URL conversion."""
        kwargs.setdefault("timeout", 30)
        return self.session.post(url, **kwargs)

    # --- Private authentication steps ---

    def _get_auth_context(self) -> tuple[str, str]:
        """Step 1: GET authenticate endpoint, extract lck from redirect."""
        service_url = f"{config.WEBVPN_BASE}/login?cas_login=true"
        url = (
            f"{config.IDP_BASE}/idp/authCenter/authenticate"
            f"?service={quote(service_url, safe='')}"
        )
        resp = self.session.get(url, allow_redirects=False, timeout=30)

        # Follow redirects manually to extract lck
        location = resp.headers.get("Location", "")
        while resp.status_code in (301, 302) and "lck=" not in location:
            resp = self.session.get(location, allow_redirects=False, timeout=30)
            location = resp.headers.get("Location", "")

        if resp.status_code in (301, 302):
            location = resp.headers.get("Location", "")

        # Extract lck parameter
        lck_match = re.search(r"[?&]lck=([^&]+)", location)
        if not lck_match:
            raise RuntimeError(
                f"Failed to extract lck from redirect (status={resp.status_code})"
            )

        lck = lck_match.group(1)
        entity_id = config.WEBVPN_BASE
        print("    lck: OK")
        return lck, entity_id

    def _idp(self):
        return FudanIDP(
            lambda method, url, **kwargs: getattr(self.session, method.lower())(url, **kwargs)
        )

    def _query_auth_methods(self, lck: str, entity_id: str) -> tuple[str, str]:
        return self._idp().query_methods(lck, entity_id)

    def _get_public_key(self) -> str:
        return self._idp().public_key()

    def _encrypt_password(self, password: str, pub_key_b64: str) -> str:
        return encrypt_password(password, pub_key_b64)

    def _auth_execute(self, student_id, encrypted_password, lck, entity_id,
                      auth_chain_code, request_type):
        return self._idp().execute(student_id, encrypted_password, lck, entity_id,
                                   auth_chain_code, request_type)

    def _get_cas_ticket(self, login_token: str) -> str:
        return self._idp().ticket(login_token)

    def _establish_session(self, ticket_url: str):
        """Step 7: Follow the ticket URL to establish WebVPN session.

        The WebVPN server can be very slow. We first try without following
        redirects to capture the session cookie quickly, then verify.
        """
        for attempt in range(3):
            try:
                resp = self.session.get(
                    ticket_url, allow_redirects=True, timeout=90
                )
                if resp.status_code == 200:
                    print("    Session established.")
                    return
                raise RuntimeError(
                    f"Failed to establish WebVPN session (status={resp.status_code})"
                )
            except requests.exceptions.Timeout:
                # Check if session was established despite timeout
                # (server may have set cookies before the timeout)
                has_ticket = any(
                    "wengine_vpn_ticket" in c.name
                    for c in self.session.cookies
                )
                if has_ticket:
                    print("    Session cookie set despite timeout.")
                    return
                if attempt < 2:
                    print(f"    Timeout, retrying ({attempt + 2}/3)...")
                    continue
                raise
