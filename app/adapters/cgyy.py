from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from app.upstream.sso import AuthenticationRequired, BuaaSsoSession

from .base import ConnectorManifest, SourceAdapter


BASE_URL = "https://cgyy.buaa.edu.cn/venue-zhjs-server"
MANAGE_LOGIN_URL = f"{BASE_URL}/sso/manageLogin"
BUSINESS_LOGIN_URL = f"{BASE_URL}/api/login"
ORDERS_URL = f"{BASE_URL}/api/orders/mine"
SSO_COOKIE_NAME = "sso_buaa_zhjs_token"
SIGN_PREFIX = "c640ca392cd45fb3a55b00a63a86c618"
APP_KEY = "8fceb735082b5a529312040b58ea780b"


class CgyyAdapter(SourceAdapter):
    id = "cgyy"
    label = "体育场馆"
    manifest = ConnectorManifest(
        id=id,
        label=label,
        version="2026-09-19",
        capabilities=("venue_bookings", "approval_status"),
        endpoints=(MANAGE_LOGIN_URL, BUSINESS_LOGIN_URL, ORDERS_URL),
        references=(
            "BUAASubnet/UBAA:CgyyZhjsClient.kt",
            "BUAASubnet/UBAA:CgyySigner.kt",
            "BUAASubnet/UBAA:CgyyService.kt",
        ),
    )

    def __init__(self, session: BuaaSsoSession):
        self.session = session
        self.access_token = ""

    async def sync(self) -> list[dict[str, Any]]:
        if not self.session.authenticated:
            raise AuthenticationRequired("需要登录北航统一认证")
        await self._login()
        payload = await self._request("GET", "/api/orders/mine", params={"page": 0, "size": 100})
        rows = payload.get("content") or []
        if not isinstance(rows, list):
            raise RuntimeError("体育场馆预约字段结构已变化")
        return [self._event(row) for row in rows if isinstance(row, dict)]

    async def _login(self) -> None:
        response = await self.session.client.get(MANAGE_LOGIN_URL)
        if response.status_code in {401, 403} or "sso.buaa.edu.cn" in str(response.url):
            raise AuthenticationRequired("体育场馆授权未完成")
        sso_token = self._cookie(SSO_COOKIE_NAME)
        if not sso_token:
            raise AuthenticationRequired("未获取到体育场馆 SSO Token")
        result = await self._request(
            "POST", "/api/login", extra_headers={"Sso-Token": sso_token}, authorize=False
        )
        token = result.get("token") if isinstance(result.get("token"), dict) else {}
        self.access_token = str(token.get("access_token") or "").strip()
        if not self.access_token:
            raise AuthenticationRequired("体育场馆登录成功但未返回 access_token")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        authorize: bool = True,
    ) -> dict[str, Any]:
        timestamp = int(datetime.now().timestamp() * 1000)
        query = dict(params or {})
        values = dict(form or {})
        if method == "GET" and "nocache" not in query:
            query["nocache"] = timestamp
        sign = self.sign(path, query if method == "GET" else values, timestamp)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://cgyy.buaa.edu.cn/venue-zhjs/mobileReservation",
            "app-key": APP_KEY,
            "timestamp": str(timestamp),
            "sign": sign,
            **(extra_headers or {}),
        }
        if authorize:
            headers["cgAuthorization"] = self.access_token
        response = await self.session.client.request(
            method,
            f"{BASE_URL}{path if path.startswith('/') else f'/{path}'}",
            params=query if method == "GET" else None,
            data=values if method != "GET" else None,
            headers=headers,
        )
        if response.status_code in {401, 403} or "sso.buaa.edu.cn" in str(response.url):
            raise AuthenticationRequired("体育场馆会话已失效")
        if response.status_code != 200:
            raise RuntimeError(f"体育场馆接口返回 {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("体育场馆接口返回了非 JSON 内容") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("体育场馆接口字段结构已变化")
        if self._int(payload.get("code")) != 200:
            raise RuntimeError(str(payload.get("message") or "体育场馆接口业务错误"))
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def _event(self, row: dict[str, Any]) -> dict[str, Any]:
        order_id = str(row.get("id") or row.get("tradeNo") or "")
        venue = self._first_text(row, "venueName", "campusName")
        site = self._first_text(row, "siteName", "venueSpaceName")
        title_detail = " · ".join(value for value in (venue, site) if value)
        order_status, check_status = self._int(row.get("orderStatus")), self._int(row.get("checkStatus"))
        if check_status is not None and check_status < 0:
            status = "rejected"
        elif order_status == 2:
            status = "cancelled"
        elif check_status == 1:
            status = "confirmed"
        elif check_status is not None and check_status > 1:
            status = "pending"
        else:
            status = "upcoming"
        return {
            "source": self.id,
            "external_id": f"venue-{order_id}",
            "kind": "venue",
            "title": f"场馆预约{f' · {title_detail}' if title_detail else ''}",
            "starts_at": self._date_time(row.get("reservationStartDate")),
            "ends_at": self._date_time(row.get("reservationEndDate")),
            "location": " · ".join(value for value in (row.get("campusName"), venue, site) if value) or None,
            "status": status,
            "metadata": {**row, "order_id": order_id, "approval_status": check_status},
        }

    def _cookie(self, name: str) -> str | None:
        for cookie in self.session.client.cookies.jar:
            if cookie.name == name and cookie.value:
                return cookie.value
        return None

    @staticmethod
    def sign(path: str, params: dict[str, Any], timestamp: int) -> str:
        normalized = path if path.startswith("/") else f"/{path}"
        ignored = {"gmtCreate", "gmtModified", "creator", "modifier", "id", "_index", "_rowKey"}
        clean = {
            key: value for key, value in params.items()
            if key not in ignored and value is not None and not isinstance(value, (list, tuple, dict, set))
            and not (isinstance(value, str) and not value)
        }
        parts = [SIGN_PREFIX, normalized]
        for key in sorted(clean):
            value = clean[key]
            parts.extend((key, str(value).lower() if isinstance(value, bool) else str(value)))
        parts.extend((str(timestamp), " ", SIGN_PREFIX))
        return hashlib.md5("".join(parts).encode("utf-8")).hexdigest()

    @staticmethod
    def _date_time(value: Any) -> str | None:
        if not value:
            return None
        raw = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw.replace(" ", "T"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return parsed.isoformat()

    @staticmethod
    def _int(value: Any) -> int | None:
        try:
            return int(value) if value is not None and str(value).strip() else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _first_text(row: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = row.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""
