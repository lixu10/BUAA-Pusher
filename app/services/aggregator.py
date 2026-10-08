from __future__ import annotations

import json
import asyncio
from datetime import UTC, datetime

from app.adapters.base import SourceAdapter
from app.db import Database
from app.upstream.sso import AuthenticationRequired
from app.services.sync_changes import SyncChangeTracker


class Aggregator:
    def __init__(self, db: Database, user_id: int, adapters: list[SourceAdapter]):
        self.db = db
        self.user_id = user_id
        self.adapters = adapters

    async def sync(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for adapter in self.adapters:
            try:
                events = await asyncio.wait_for(adapter.sync(), timeout=180)
                previous_source = self.db.get_source(self.user_id, adapter.id)
                source_has_baseline = previous_source is not None and previous_source.get("status") in {"healthy", "degraded"}
                for event in events:
                    if event.pop("_change_event", False):
                        previous = self.db.event_by_key(
                            self.user_id, event["source"], event["external_id"]
                        )
                        if previous is None and not source_has_baseline:
                            event["starts_at"] = None
                        elif previous is not None and self._same_change_state(previous, event):
                            event["starts_at"] = previous.get("starts_at")
                        else:
                            event["starts_at"] = datetime.now(UTC).isoformat()
                    self.db.upsert_event(self.user_id, event)
                error_count = int(getattr(adapter, "last_error_count", 0))
                error_detail = str(getattr(adapter, "last_error_detail", ""))
                # Store ignored flags in the comparison snapshot; source sync must
                # never clear a user's local override.
                snapshots = [self.db.event_by_key(self.user_id, adapter.id, event["external_id"])
                             for event in events]
                SyncChangeTracker(self.db, self.user_id).record(
                    adapter.id, [event for event in snapshots if event], not error_count)
                if not error_count:
                    self.db.remove_missing_source_events(
                        self.user_id, adapter.id,
                        {str(event["external_id"]) for event in events},
                        kinds=getattr(adapter, "cleanup_kinds", None),
                    )
                self.db.upsert_source(self.user_id, {
                    "id": adapter.id, "label": adapter.label,
                    "status": "degraded" if error_count else "healthy",
                    "detail": error_detail or (f"{error_count} 个路由不可用" if error_count else ""),
                    "last_sync_at": datetime.now(UTC).isoformat(),
                    "event_count": len(events),
                })
                result[adapter.id] = len(events)
            except AuthenticationRequired as exc:
                self.db.upsert_source(self.user_id, {
                    "id": adapter.id, "label": adapter.label, "status": "login_required",
                    "detail": self._error_detail(exc), "last_sync_at": datetime.now(UTC).isoformat(),
                    "event_count": 0,
                })
                result[adapter.id] = 0
            except Exception as exc:
                self.db.upsert_source(self.user_id, {
                    "id": adapter.id, "label": adapter.label, "status": "error",
                    "detail": self._error_detail(exc), "last_sync_at": datetime.now(UTC).isoformat(),
                    "event_count": 0,
                })
                result[adapter.id] = 0
        return result

    @staticmethod
    def _error_detail(exc: Exception) -> str:
        if isinstance(exc, TimeoutError):
            return "同步超时，已保留上次数据，可单独重试"
        message = str(exc).strip()
        return (message or exc.__class__.__name__)[:160]

    @staticmethod
    def _same_change_state(previous: dict, current: dict) -> bool:
        return (
            previous.get("title") == current.get("title")
            and previous.get("status") == current.get("status")
            and json.dumps(previous.get("metadata") or {}, sort_keys=True, ensure_ascii=False)
            == json.dumps(current.get("metadata") or {}, sort_keys=True, ensure_ascii=False)
        )

    async def close(self) -> None:
        for adapter in self.adapters:
            await adapter.close()
