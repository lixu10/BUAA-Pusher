from __future__ import annotations

import asyncio

from app.upstream import BuaaSsoSession


class SchoolSessionManager:
    """Keeps upstream cookies isolated per PUAA user."""

    def __init__(self, trust_env: bool = False):
        self.trust_env = trust_env
        self._sessions: dict[int, BuaaSsoSession] = {}
        self._lock = asyncio.Lock()

    async def get(self, user_id: int) -> BuaaSsoSession:
        async with self._lock:
            if user_id not in self._sessions:
                self._sessions[user_id] = BuaaSsoSession(trust_env=self.trust_env)
            return self._sessions[user_id]

    async def remove(self, user_id: int) -> None:
        async with self._lock:
            session = self._sessions.pop(user_id, None)
        if session:
            await session.logout()
            await session.close()

    async def close(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.close()
