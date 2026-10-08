from __future__ import annotations

import hashlib
import asyncio
import time as clock_time
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx

from app.upstream.sso import AuthenticationRequired, BuaaSsoSession

from .base import ConnectorManifest, SourceAdapter


MY_CENTER_URL = "https://iclass.buaa.edu.cn:8346/?type=jumpMyCenter"
LOGIN_URL = "https://iclass.buaa.edu.cn:8347/app/user/login.action"
CLASSES_URL = "https://iclass.buaa.edu.cn:8347/app/course/get_stu_course_sched.action"


class IClassAdapter(SourceAdapter):
    id = "iclass"
    label = "课程签到"
    manifest = ConnectorManifest(
        id=id,
        label=label,
        version="2026-09-19",
        capabilities=("signin_status",),
        endpoints=(MY_CENTER_URL, LOGIN_URL, CLASSES_URL),
        references=(
            "BUAASubnet/UBAA:SigninClient.kt",
            "BUAASubnet/UBAA:SigninLoginNameSupport.kt",
            "singledog957/duaa",
        ),
    )

    def __init__(self, session: BuaaSsoSession):
        self.session = session

    async def sync(self) -> list[dict[str, Any]]:
        if not self.session.authenticated:
            raise AuthenticationRequired("需要登录北航统一认证")
        for attempt in range(2):
            try:
                return await self._sync_session()
            except AuthenticationRequired:
                self.session.iclass_identity = None
                if attempt:
                    raise
                await asyncio.sleep(0.2)
        return []

    async def _sync_session(self) -> list[dict[str, Any]]:
        cached = getattr(self.session, "iclass_identity", None)
        if cached and clock_time.monotonic() - cached[3] < 1800:
            login_name, user_id, session_id, _ = cached
        else:
            login_name = await self._resolve_login_name()
            user_id = session_id = ""
        # Match iClass itself: its app session is isolated from the SSO cookie
        # jar and its certificate chain is not consistently trusted on Windows.
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=30, trust_env=False, verify=False,
            headers={"User-Agent": "Mozilla/5.0 PUAA-Reminder/0.1"},
        ) as client:
            if not session_id:
                user_id, session_id = await self._app_login(client, login_name)
            self.session.iclass_identity = (login_name, user_id, session_id, clock_time.monotonic())
            events: list[dict[str, Any]] = []
            for offset in range(2):
                day = datetime.now(timezone(timedelta(hours=8))).date() + timedelta(days=offset)
                self.report_progress(offset, 2, "查询签到详情")
                class_payload = await self._classes(client, user_id, session_id, day)
                if "STATUS" in class_payload and not self._success(class_payload):
                    # UBAA retries once because iClass sometimes invalidates a
                    # freshly-issued session during the first data request.
                    user_id, session_id = await self._app_login(client, login_name)
                    self.session.iclass_identity = (login_name, user_id, session_id, clock_time.monotonic())
                    class_payload = await self._classes(client, user_id, session_id, day)
                    if "STATUS" in class_payload and not self._success(class_payload):
                        message = str(class_payload.get("ERRMSG") or "").strip()
                        if any(word in message for word in ("登录", "会话", "认证", "失效")):
                            raise AuthenticationRequired(message)
                        # Current iClass returns STATUS=2 with an empty result
                        # after a successful app login when no schedule can be
                        # read for that day.  UBAA treats this as an empty day.
                        continue
                rows = class_payload.get("result") or []
                if not isinstance(rows, list):
                    raise RuntimeError("iClass 课程字段结构已变化")
                events.extend(self._event(day, item) for item in rows if isinstance(item, dict))
            self.report_progress(2, 2, "查询签到详情")
            return events

    async def _app_login(self, client: httpx.AsyncClient, login_name: str) -> tuple[str, str]:
        response = await client.get(LOGIN_URL, params={
            "password": "", "phone": login_name, "userLevel": "1",
            "verificationType": "2", "verificationUrl": "",
        })
        payload = self._payload(response, "iClass 登录")
        if not self._success(payload):
            raise AuthenticationRequired(str(payload.get("ERRMSG") or "iClass 登录失败"))
        identity = payload.get("result") or {}
        user_id, session_id = identity.get("id"), identity.get("sessionId")
        if not user_id or not session_id:
            raise RuntimeError("iClass 登录响应缺少会话字段")
        return str(user_id), str(session_id)

    async def _classes(
        self, client: httpx.AsyncClient, user_id: str, session_id: str, day: date
    ) -> dict[str, Any]:
        response = await client.get(
            CLASSES_URL,
            params={"id": user_id, "dateStr": day.strftime("%Y%m%d")},
            headers={"sessionId": session_id},
        )
        return self._payload(response, "iClass 课程")

    async def _resolve_login_name(self) -> str:
        current = MY_CENTER_URL
        for _ in range(8):
            found = self._login_name(current)
            if found:
                return found
            response = await self.session.client.get(current, follow_redirects=False)
            found = self._login_name(str(response.url))
            if found:
                return found
            location = response.headers.get("location")
            if location:
                target = urljoin(str(response.url), location)
                found = self._login_name(target)
                if found:
                    return found
                current = target
                continue
            if "sso.buaa.edu.cn" in str(response.url) or response.status_code == 401:
                raise AuthenticationRequired("iClass 授权未完成")
            break
        raise AuthenticationRequired("iClass 授权跳转未返回登录凭据，请单独重试或重新连接北航")

    @staticmethod
    def _login_name(url: str) -> str | None:
        # loginName may contain '+'. parse_qs treats that as a space, while
        # iClass expects the literal character (UBAA percent-decodes only %xx).
        for part in urlparse(url).query.split("&"):
            key, separator, value = part.partition("=")
            if separator and unquote(key).lower() == "loginname" and value:
                return unquote(value)
        return None

    @staticmethod
    def _success(payload: dict[str, Any]) -> bool:
        return str(payload.get("STATUS", "")).lower() in {"0", "200", "success"}

    @staticmethod
    def _payload(response: Any, label: str) -> dict[str, Any]:
        if response.status_code == 401 or "sso.buaa.edu.cn" in str(response.url):
            raise AuthenticationRequired(f"{label}授权未完成")
        if response.status_code != 200:
            raise RuntimeError(f"{label}返回 {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{label}返回了非 JSON 内容") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{label}字段结构已变化")
        return payload

    def _event(self, day: date, item: dict[str, Any]) -> dict[str, Any]:
        course_id = str(item.get("id") or "")
        name = str(item.get("courseName") or "课程签到")
        begins = self._combine(day, item.get("classBeginTime"))
        ends = self._combine(day, item.get("classEndTime"))
        raw_status = str(item.get("signStatus") if item.get("signStatus") is not None else "").strip()
        signed = raw_status == "1"
        identity = course_id or hashlib.sha1(f"{day}|{name}|{begins}".encode()).hexdigest()[:16]
        return {
            "source": self.id,
            "external_id": f"signin-{day.isoformat()}-{identity}",
            "kind": "signin",
            "title": f"{name}签到",
            "starts_at": begins,
            "ends_at": ends,
            "status": "done" if signed else "missing" if raw_status == "0" else "unknown",
            "metadata": {**item, "signed": signed, "course_name": name},
        }

    @staticmethod
    def _combine(day: date, value: Any) -> str | None:
        if not value:
            return None
        raw = str(value).strip()
        try:
            parsed = datetime.fromisoformat(raw.replace(" ", "T"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
            return parsed.isoformat()
        except ValueError:
            pass
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                parsed_time = datetime.strptime(raw, fmt).time()
                return datetime.combine(day, time(parsed_time.hour, parsed_time.minute, parsed_time.second), timezone(timedelta(hours=8))).isoformat()
            except ValueError:
                continue
        return None
