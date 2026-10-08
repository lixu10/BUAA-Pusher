from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

import httpx
import certifi
from app.upstream.webvpn import WebVpnClient, from_webvpn_url


SSO_URL = "https://sso.buaa.edu.cn/login"
UC_LOGIN_URL = (
    "https://uc.buaa.edu.cn/api/login?"
    "target=https%3A%2F%2Fuc.buaa.edu.cn%2F%23%2Fuser%2Flogin"
)
UC_STATUS_URL = "https://uc.buaa.edu.cn/api/uc/status"


class AuthenticationRequired(RuntimeError):
    pass


class LoginFailed(RuntimeError):
    pass


class _LoginFormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_form = False
        self.found_form = False
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form" and not self.found_form:
            form_id = values.get("id")
            if form_id in {"fm1", "loginForm", None}:
                self.in_form = True
                self.found_form = True
        if tag != "input" or not self.in_form:
            return
        name = values.get("name")
        input_type = (values.get("type") or "text").lower()
        if not name or input_type in {"submit", "button", "image", "password"}:
            return
        if input_type == "checkbox" and "checked" not in values:
            return
        self.fields[name] = values.get("value") or ("on" if input_type == "checkbox" else "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.in_form:
            self.in_form = False


def parse_login_form(html: str) -> dict[str, str]:
    parser = _LoginFormParser()
    parser.feed(html)
    return parser.fields


def parse_captcha_id(html: str) -> str | None:
    match = re.search(
        r"config\.captcha\s*=\s*\{\s*type:\s*['\"][^'\"]+['\"],\s*id:\s*['\"]([^'\"]+)",
        html,
    )
    return match.group(1) if match else None


def parse_login_error(html: str) -> str | None:
    patterns = (
        r'<div class="tip-text">([^<]+)</div>',
        r'<div[^>]+class="[^"]*errors[^"]*"[^>]*>(.*?)</div>',
        r'<div[^>]+id="errorDiv"[^>]*>.*?<p[^>]*>(.*?)</p>.*?</div>',
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if match:
            return re.sub(r"<[^>]+>", "", match.group(1)).strip()
    return None


class BuaaSsoSession:
    def __init__(self, trust_env: bool = False, network_mode: str = "direct"):
        if network_mode not in {"direct", "webvpn"}:
            raise ValueError("不支持的访问模式")
        self.network_mode = network_mode
        self.client = WebVpnClient(trust_env) if network_mode == "webvpn" else httpx.AsyncClient(
            follow_redirects=True,
            timeout=20,
            trust_env=trust_env,
            verify=certifi.where(),
            headers={
                "User-Agent": "Mozilla/5.0 PUAA-Reminder/0.1",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        self.username: str | None = None
        self.user: dict[str, Any] | None = None
        self.iclass_identity: tuple[str, str, str, float] | None = None
        self._form: dict[str, str] = {}
        self._captcha_id: str | None = None
        self._login_url = SSO_URL

    @property
    def authenticated(self) -> bool:
        return self.user is not None

    async def preload(self) -> dict[str, Any]:
        response = await self.client.get(SSO_URL)
        response.raise_for_status()
        self._form = parse_login_form(response.text)
        self._captcha_id = parse_captcha_id(response.text)
        if self.network_mode == "webvpn":
            from urllib.parse import urlsplit
            target = urlsplit(from_webvpn_url(str(response.url)))
            if target.hostname != "sso.buaa.edu.cn" or target.path != "/login" or not self._form.get("execution"):
                raise LoginFailed("学校 WebVPN 登录入口不可用，请稍后重试")
            self._login_url = str(response.url)
        return {
            "captcha_required": self._captcha_id is not None,
            "captcha_id": self._captcha_id,
        }

    async def captcha(self, captcha_id: str) -> tuple[bytes, str]:
        response = await self.client.get("https://sso.buaa.edu.cn/captcha", params={"captchaId": captcha_id})
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "image/jpeg")

    async def login(self, username: str, password: str, captcha: str | None = None) -> dict[str, Any]:
        if self.username != username:
            self.iclass_identity = None
        if not self._form:
            await self.preload()
        if self._captcha_id and not captcha:
            return {"authenticated": False, "captcha_required": True, "captcha_id": self._captcha_id}

        form = dict(self._form)
        form.update({
            "username": username,
            "password": password,
            "submit": "LOGIN",
            "_eventId": form.get("_eventId") or "submit",
            "type": form.get("type") or "username_password",
        })
        if captcha:
            form["captcha"] = captcha
            form["captchaResponse"] = captcha
        response = await self.client.post(self._login_url, data=form)
        error = parse_login_error(response.text)
        if error or ("name=\"execution\"" in response.text and "password" in response.text.lower()):
            self._form = {}
            raise LoginFailed(error or "账号或密码错误")

        if "ignoreAndContinue" in response.text or "continueForm" in response.text:
            expiry_form = parse_login_form(response.text)
            execution = expiry_form.get("execution")
            if execution:
                await self.client.post(str(response.url).split("?", 1)[0], data={
                    "execution": execution,
                    "_eventId": "ignoreAndContinue",
                })

        await self.client.get(UC_LOGIN_URL)
        status = await self.refresh_status()
        if not status["authenticated"]:
            self._form = {}
            raise LoginFailed("统一认证成功，但用户中心会话校验失败")
        self.username = username
        self._form = {}
        return status

    async def refresh_status(self) -> dict[str, Any]:
        try:
            response = await self.client.get(
                UC_STATUS_URL,
                headers={"Accept": "application/json, text/javascript, */*; q=0.01", "X-Requested-With": "XMLHttpRequest"},
            )
            payload = response.json()
            if response.status_code == 200 and payload.get("code") == 0 and isinstance(payload.get("data"), dict):
                self.user = payload["data"]
                return {"authenticated": True, "user": self.user}
        except (httpx.HTTPError, ValueError):
            pass
        self.user = None
        return {"authenticated": False, "user": None}

    async def logout(self) -> None:
        try:
            await self.client.get("https://sso.buaa.edu.cn/logout")
        finally:
            self.client.cookies.clear()
            self.username = None
            self.iclass_identity = None
            self.user = None
            self._form = {}
            self._captcha_id = None

    async def close(self) -> None:
        await self.client.aclose()
