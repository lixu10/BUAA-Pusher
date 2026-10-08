from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable

from app.services.notifications import TriggerDispatcher


class BackgroundWorker:
    def __init__(
        self,
        dispatcher: TriggerDispatcher,
        sync_all: Callable[[], Awaitable[None]] | None = None,
        interval: int = 15,
        sync_interval: int = 900,
    ):
        self.dispatcher = dispatcher
        self.sync_all = sync_all
        self.interval = interval
        self.sync_interval = sync_interval
        self.task: asyncio.Task | None = None

    async def run(self) -> None:
        next_sync = time.monotonic() + self.sync_interval
        while True:
            try:
                await self.dispatcher.dispatch_due()
            except Exception:
                pass
            if self.sync_all and time.monotonic() >= next_sync:
                try:
                    await self.sync_all()
                except Exception:
                    pass
                next_sync = time.monotonic() + self.sync_interval
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        self.task = asyncio.create_task(self.run(), name="puaa-notification-worker")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
