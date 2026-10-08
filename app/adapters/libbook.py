from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

from app.upstream.sso import AuthenticationRequired, BuaaSsoSession

from .base import ConnectorManifest, SourceAdapter


BASE_URL = "https://booking.lib.buaa.edu.cn"
CAS_LOGIN_URL = (
    "https://sso.buaa.edu.cn/login?service="
    "https%3A%2F%2Fbooking.lib.buaa.edu.cn%2Fv4%2Flogin%2Fcas"
)
USER_LOGIN_URL = f"{BASE_URL}/v4/login/user"
BOOKINGS_URL = f"{BASE_URL}/v4/member/seat"


class LibBookAdapter(SourceAdapter):
    id = "libbook"
    label = "图书馆预约"
    manifest = ConnectorManifest(
        id=id,
        label=label,
        version="2026-09-19",
        capabilities=("seat_bookings", "booking_status"),
        endpoints=(CAS_LOGIN_URL, USER_LOGIN_URL, BOOKINGS_URL),
        references=(
            "BUAASubnet/UBAA:LibBookClient.kt",
            "BUAASubnet/UBAA:LibBookService.kt",
        ),
    )

    def __init__(self, session: BuaaSsoSession):
        self.session = session
        self.token = ""
        self.library_client: httpx.AsyncClient | None = None

    async def sync(self) -> list[dict[str, Any]]:
        if not self.session.authenticated:
            raise AuthenticationRequired("需要登录北航统一认证")
        # The library host currently serves an incomplete certificate chain on
        # some Windows installations.  Keep SSO verification strict and scope
        # the compatibility client to booking.lib.buaa.edu.cn only.
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=20, trust_env=False, verify=False,
            headers={"User-Agent": "Mozilla/5.0 PUAA-Reminder/0.1"},
        ) as library_client:
            self.library_client = library_client
            cas = await self._cas_token()
            login = await self._post(USER_LOGIN_URL, {"cas": cas}, authorize=False)
            data = login.get("data") if isinstance(login.get("data"), dict) else {}
            raw_member = data.get("member")
            if isinstance(raw_member, list):
                member = next((item for item in raw_member if isinstance(item, dict)), {})
            else:
                member = raw_member if isinstance(raw_member, dict) else {}
            self.token = str(member.get("token") or "").strip()
            if not self.token:
                raise AuthenticationRequired("图书馆登录成功但未返回业务 token")

            payload = await self._post(BOOKINGS_URL, {"type": "1", "page": 1, "limit": 100})
            page = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            rows = page.get("data") if isinstance(page.get("data"), list) else page.get("list")
            if rows is None:
                rows = []
            if not isinstance(rows, list):
                raise RuntimeError("图书馆预约字段结构已变化")
            return [self._event(row) for row in rows if isinstance(row, dict)]

    async def _cas_token(self) -> str:
        current = CAS_LOGIN_URL
        for _ in range(8):
            found = self.extract_cas(current)
            if found:
                return found
            client = self.library_client if current.startswith(BASE_URL) else self.session.client
            response = await client.get(current, follow_redirects=False, headers=self._headers())
            found = self.extract_cas(str(response.url))
            location = response.headers.get("location")
            if not found and location:
                current = urljoin(str(response.url), location)
                found = self.extract_cas(current)
            if found:
                return found
            if not location:
                if response.status_code in {401, 403} or "sso.buaa.edu.cn" in str(response.url):
                    raise AuthenticationRequired("图书馆授权未完成")
                break
        raise AuthenticationRequired("未能从图书馆登录链获取 CAS 参数")

    async def _post(
        self, url: str, body: dict[str, Any], *, authorize: bool = True
    ) -> dict[str, Any]:
        headers = self._headers()
        if authorize:
            if not self.token:
                raise AuthenticationRequired("图书馆业务 token 缺失")
            headers["Authorization"] = f"bearer{self.token}"
        if self.library_client is None:
            raise RuntimeError("图书馆连接尚未初始化")
        response = await self.library_client.post(url, json=body, headers=headers)
        if response.status_code in {401, 403} or "sso.buaa.edu.cn" in str(response.url):
            raise AuthenticationRequired("图书馆会话已失效")
        if response.status_code != 200:
            raise RuntimeError(f"图书馆接口返回 {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("图书馆接口返回了非 JSON 内容") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("图书馆接口字段结构已变化")
        message = str(payload.get("message") or payload.get("msg") or "")
        if any(word in message for word in ("登录失效", "请重新登录", "未登录")):
            raise AuthenticationRequired(message)
        code = self._int(payload.get("code"))
        if code is not None and code not in {0, 1}:
            raise RuntimeError(message or "图书馆接口业务错误")
        return payload

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE_URL,
            "Origin": BASE_URL,
        }

    def _event(self, row: dict[str, Any]) -> dict[str, Any]:
        booking_id = self._text(row, "id")
        area = self._text(row, "name", "area_name")
        merged = self._text(row, "nameMerge", "name_merge")
        seat = self._text(row, "no", "seat_no")
        day = self._text(row, "day", "date")
        begin = self._text(row, "beginTime", "begin_time")
        end = self._text(row, "endTime", "end_time")
        status_code = self._text(row, "status")
        status_name = self._text(row, "status_name")
        done = status_code in {"6", "8"} or any(
            word in status_name for word in ("取消", "结束", "完成", "过期", "失效")
        )
        identity = booking_id or hashlib.sha1(
            f"{day}|{begin}|{end}|{area}|{seat}".encode()
        ).hexdigest()[:16]
        title_detail = " · ".join(value for value in (area or merged, seat) if value)
        return {
            "source": self.id,
            "external_id": f"booking-{identity}",
            "kind": "booking",
            "title": f"图书馆预约{f' · {title_detail}' if title_detail else ''}",
            "starts_at": self._date_time(day, begin),
            "ends_at": self._date_time(day, end),
            "status": "done" if done else "upcoming",
            "location": area or merged or None,
            "metadata": {
                **row,
                "booking_id": booking_id or None,
                "area_name": area or merged or None,
                "seat_no": seat or None,
                "status_name": status_name or None,
            },
        }

    @staticmethod
    def extract_cas(url: str) -> str | None:
        values = parse_qs(urlparse(url).query)
        found = (values.get("cas") or [None])[0]
        if found:
            return unquote(found)
        # The current H5 app places its route in the URL fragment, e.g.
        # index.html#/cas?cas=..., so urlparse(...).query is empty.
        match = re.search(r"[?&#]cas=([^&]+)", url)
        return unquote(match.group(1)) if match else None

    @staticmethod
    def _date_time(day: str, value: str) -> str | None:
        if not day:
            return None
        raw = f"{day} {value}".strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw.replace(" ", "T", 1))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return parsed.isoformat()

    @staticmethod
    def _text(row: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = row.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @staticmethod
    def _int(value: Any) -> int | None:
        try:
            return int(value) if value is not None and str(value).strip() else None
        except (TypeError, ValueError):
            return None
