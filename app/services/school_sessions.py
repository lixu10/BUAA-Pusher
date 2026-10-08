from __future__ import annotations

import asyncio

from app.upstream import BuaaSsoSession


class SchoolSessionManager:
    """Keeps upstream cookies isolated per PUAA user."""

    def __init__(self, trust_env: bool = False):
        self.trust_env = trust_env
        self._sessions: dict[int, BuaaSsoSession] = {}
        self._webvpn_sessions: dict[int, BuaaSsoSession] = {}
        self._lock = asyncio.Lock()

    async def get(self, user_id: int) -> BuaaSsoSession:
        async with self._lock:
            if user_id not in self._sessions:
                self._sessions[user_id] = BuaaSsoSession(trust_env=self.trust_env)
            return self._sessions[user_id]

    async def get_webvpn(self, user_id: int) -> BuaaSsoSession:
        async with self._lock:
            if user_id not in self._webvpn_sessions:
                self._webvpn_sessions[user_id] = BuaaSsoSession(trust_env=self.trust_env, network_mode="webvpn")
            return self._webvpn_sessions[user_id]

    def webvpn_authenticated(self, user_id: int) -> bool:
        session = self._webvpn_sessions.get(user_id)
        return bool(session and session.authenticated)

    async def reset_webvpn(self, user_id: int) -> None:
        async with self._lock:
            session = self._webvpn_sessions.pop(user_id, None)
        if session:
            await session.close()

    async def remove(self, user_id: int) -> None:
        async with self._lock:
            session = self._sessions.pop(user_id, None)
            vpn_session = self._webvpn_sessions.pop(user_id, None)
        if session:
            await session.logout()
            await session.close()
        if vpn_session:
            await vpn_session.close()  # No extra school logout/write when clearing this client.

    async def close(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values()) + list(self._webvpn_sessions.values())
            self._sessions.clear()
            self._webvpn_sessions.clear()
        for session in sessions:
            await session.close()
