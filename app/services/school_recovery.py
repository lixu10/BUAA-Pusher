"""Bounded recovery for readonly school queries, isolated by user and route."""
from __future__ import annotations

import asyncio
import time

from app.upstream.sso import AuthenticationFlowError, AuthenticationRequired, LoginFailed


class SchoolAuthRecovery:
    def __init__(self):
        self.locks = {}

    def lock(self, user_id, mode):
        return self.locks.setdefault((user_id, mode), asyncio.Lock())

    @staticmethod
    def invalidate(session):
        # Local invalidation only: do not call the school's logout endpoint.
        session.user = None
        session.iclass_identity = None
        session.client.cookies.clear()
        session._form = {}
        session._captcha_id = None

    @staticmethod
    def check_block(session):
        blocked = getattr(session, "reauthentication_error", None)
        if isinstance(blocked, str) and blocked:
            until = getattr(session, "reauthentication_retry_at", None)
            if until is None or time.monotonic() < until:
                raise AuthenticationRequired(blocked)

    async def authenticate(self, session, credentials):
        if session.authenticated:
            return
        self.check_block(session)
        secret = credentials()
        if not secret:
            raise AuthenticationRequired("学校会话已失效，请重新登录或启用记住密码")
        username, password = secret
        try:
            status = await session.login(username, password)
        except AuthenticationFlowError as exc:
            session._form = {}
            session._captcha_id = None
            session.reauthentication_error = "学校认证跳转或用户中心会话建立失败，旧数据已保留，请稍后重试"
            session.reauthentication_retry_at = time.monotonic() + 60
            raise AuthenticationRequired(session.reauthentication_error) from exc
        except LoginFailed as exc:
            # Never persist upstream text, credentials or authentication URLs.
            session.reauthentication_error = "学校自动认证失败，请手动重新登录确认密码或验证要求"
            session.reauthentication_retry_at = None
            raise AuthenticationRequired(session.reauthentication_error) from exc
        finally:
            password = None
            secret = None
        if not status.get("authenticated"):
            session.reauthentication_error = (
                "学校认证需要验证码，请手动重新登录" if status.get("captcha_required")
                else "学校自动认证未完成，请手动重新登录")
            session.reauthentication_retry_at = None
            raise AuthenticationRequired(session.reauthentication_error)
        session.last_authenticated_at = time.monotonic()

    async def refresh_due(self, user_id, session, credentials, hours=24):
        """Refresh authentication only; never read course/assignment/sign-in APIs."""
        async with self.lock(user_id, session.network_mode):
            if session.authenticated:
                last = getattr(session, "last_authenticated_at", None)
                if last is None:
                    session.last_authenticated_at = time.monotonic()
                    return False
                if time.monotonic() - last < hours * 3600:
                    return False
            # Lack of saved credentials does not invalidate a working manual session.
            self.check_block(session)
            secret = credentials()
            if not secret:
                return False
            secret = None
            self.invalidate(session)
            await self.authenticate(session, credentials)
            return True

    async def run(self, user_id, session, operation, credentials, on_recovery=None):
        # Serialize reads sharing cookies so a renewal cannot invalidate another
        # in-flight request. Different users and direct/WebVPN remain independent.
        mode = getattr(session, "network_mode", "direct")
        if mode not in {"direct", "webvpn"}:
            mode = "direct"
        async with self.lock(user_id, mode):
            await self.authenticate(session, credentials)
            try:
                return await operation()
            except AuthenticationRequired:
                self.invalidate(session)
                if on_recovery:
                    on_recovery()
            await self.authenticate(session, credentials)
            try:
                return await operation()  # At most one query retry.
            except AuthenticationRequired:
                self.invalidate(session)
                session.reauthentication_error = "重新认证后学校接口仍未授权，旧数据已保留，请稍后重试或手动登录"
                session.reauthentication_retry_at = time.monotonic() + 60
                raise
