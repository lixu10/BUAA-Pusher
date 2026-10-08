"""School WebVPN routing, compatible with UBAA's AES-CFB host encoding.

The public gateway-format key is not an account credential. Only the fixed
school hosts needed by JUDGE/SSO are accepted. Cookies remain in memory.
"""
from __future__ import annotations

from urllib.parse import urljoin, urlsplit, urlunsplit

import certifi
import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


GATEWAY = "d.buaa.edu.cn"
ALLOWED_HOSTS = {"judge.buaa.edu.cn", "sso.buaa.edu.cn", "uc.buaa.edu.cn"}
HOST_KEY = b"wrdvpnisthebest!"


def encode_host(host: str) -> str:
    plain = host.encode("ascii")
    encryptor = Cipher(algorithms.AES(HOST_KEY), modes.CFB(HOST_KEY)).encryptor()
    return HOST_KEY.hex() + (encryptor.update(plain) + encryptor.finalize()).hex()


def from_webvpn_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.hostname != GATEWAY:
        return url
    segments = parsed.path.split("/", 3)
    if len(segments) < 3 or segments[1].split("-", 1)[0] not in {"http", "https"}:
        return url  # Gateway login/portal, not an encoded upstream route.
    try:
        encoded = bytes.fromhex(segments[2])
        if len(encoded) <= 16:
            raise ValueError()
        decryptor = Cipher(algorithms.AES(HOST_KEY), modes.CFB(encoded[:16])).decryptor()
        host = (decryptor.update(encoded[16:]) + decryptor.finalize()).decode("ascii")
        if host not in ALLOWED_HOSTS:
            raise ValueError()
        protocol, _, port = segments[1].partition("-")
        if port and int(port) not in {80, 443}:
            raise ValueError()
        authority = host + (":" + port if port else "")
        return urlunsplit((protocol, authority, "/" + segments[3] if len(segments) == 4 else "", parsed.query, parsed.fragment))
    except (ValueError, UnicodeError):
        raise ValueError("不支持的学校 WebVPN 地址") from None


def to_webvpn_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        raise ValueError("WebVPN 地址不能包含凭据")
    if parsed.hostname == GATEWAY:
        if parsed.scheme != "https" or parsed.port not in {None, 443}:
            raise ValueError("学校 WebVPN 必须使用 HTTPS")
        from_webvpn_url(url)  # Validate encoded destinations as well.
        return url
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("WebVPN 仅允许固定的学校接口")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("不支持的学校接口端口")
    protocol = parsed.scheme
    if parsed.port and parsed.port != (443 if protocol == "https" else 80):
        protocol += "-" + str(parsed.port)
    return urlunsplit(("https", GATEWAY, f"/{protocol}/{encode_host(parsed.hostname)}{parsed.path or '/'}", parsed.query, parsed.fragment))


class WebVpnClient:
    """Small HTTPX facade: normalize every redirect before contacting a host.

    SSO credential POSTs are the only permitted writes. Never replay their body
    to a gateway portal or business endpoint on a 307/308 redirect.
    """
    def __init__(self, trust_env: bool = False, *, transport=None):
        self.raw = httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=10), follow_redirects=False,
            trust_env=trust_env, verify=certifi.where(), transport=transport,
            headers={"User-Agent": "Mozilla/5.0 PUAA-Reminder/0.1", "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        self.cookies = self.raw.cookies

    async def get(self, url: str, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs):
        return await self.request("POST", url, **kwargs)

    async def request(self, method: str, url: str, **kwargs):
        original = urlsplit(from_webvpn_url(url))
        if method not in {"GET", "POST"} or (method == "POST" and
                (original.hostname != "sso.buaa.edu.cn" or original.path != "/login")):
            raise ValueError("WebVPN 只允许查询和统一认证登录")
        current = to_webvpn_url(url)
        kwargs.pop("follow_redirects", None)
        history = []
        for _ in range(16):
            response = await self.raw.request(method, current, follow_redirects=False, **kwargs)
            if not response.is_redirect or not response.headers.get("location"):
                response.history = history
                return response
            target = to_webvpn_url(urljoin(str(response.url), response.headers["location"]))
            if method == "POST" and response.status_code in {307, 308}:
                if urlsplit(from_webvpn_url(target))._replace(query="", fragment="") != original._replace(query="", fragment=""):
                    raise ValueError("WebVPN 登录跳转异常，已停止发送凭据")
            elif response.status_code == 303 or (method == "POST" and response.status_code in {301, 302}):
                method = "GET"
                kwargs.pop("data", None)
                kwargs.pop("json", None)
                kwargs.pop("content", None)
            kwargs.pop("params", None)
            # HTTPX generates cookies for each validated gateway path itself.
            current = target
            history.append(response)
        raise RuntimeError("学校 WebVPN 跳转次数过多，请重新连接")

    async def aclose(self):
        await self.raw.aclose()
