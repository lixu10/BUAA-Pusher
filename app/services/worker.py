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
        self.dispatch_tasks: set[asyncio.Task] = set()

    async def dispatch(self) -> None:
        try:
            await self.dispatcher.dispatch_due()
        except Exception:
            pass

    async def run(self) -> None:
        next_sync = time.monotonic() + self.sync_interval
        try:
            while True:
                # Dispatcher claims user/source groups. Slow syncs can remain
                # in flight without blocking later checkpoints or other users.
                task = asyncio.create_task(self.dispatch(), name="puaa-notification-dispatch")
                self.dispatch_tasks.add(task)
                task.add_done_callback(self.dispatch_tasks.discard)
                if self.sync_all and time.monotonic() >= next_sync:
                    try:
                        await self.sync_all()
                    except Exception:
                        pass
                    next_sync = time.monotonic() + self.sync_interval
                await asyncio.sleep(self.interval)
        finally:
            for task in self.dispatch_tasks:
                task.cancel()
            await asyncio.gather(*self.dispatch_tasks, return_exceptions=True)
            self.dispatch_tasks.clear()

    def start(self) -> None:
        self.task = asyncio.create_task(self.run(), name="puaa-notification-worker")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
