import unittest
from datetime import UTC, datetime

from app.adapters.base import ConnectorManifest, SourceAdapter
from app.db import Database
from app.services.aggregator import Aggregator


class ChangeAdapter(SourceAdapter):
    id = "changes"
    label = "Changes"
    manifest = ConnectorManifest(id, label, "test", ("changes",), (), ())

    def __init__(self, score):
        self.score = score

    async def sync(self):
        return [{
            "source": self.id, "external_id": "grade-1", "kind": "grade",
            "title": "课程成绩", "starts_at": datetime.now(UTC).isoformat(),
            "status": "published", "metadata": {"score": self.score},
            "_change_event": True,
        }]


class AggregatorChangeTest(unittest.IsolatedAsyncioTestCase):
    async def test_initial_snapshot_is_silent_then_change_gets_anchor(self):
        db = Database(":memory:")
        db.init()
        user = db.create_user("change@example.com", "Change", "hash")
        await Aggregator(db, user["id"], [ChangeAdapter("80")]).sync()
        first = db.event_by_key(user["id"], "changes", "grade-1")
        self.assertIsNone(first["starts_at"])
        await Aggregator(db, user["id"], [ChangeAdapter("80")]).sync()
        stable = db.event_by_key(user["id"], "changes", "grade-1")
        self.assertIsNone(stable["starts_at"])
        await Aggregator(db, user["id"], [ChangeAdapter("95")]).sync()
        changed = db.event_by_key(user["id"], "changes", "grade-1")
        self.assertIsNotNone(changed["starts_at"])

    async def test_successful_full_sync_removes_stale_source_events(self):
        db = Database(":memory:")
        db.init()
        user = db.create_user("stale@example.com", "Stale", "hash")
        adapter = ChangeAdapter("80")
        await Aggregator(db, user["id"], [adapter]).sync()
        self.assertEqual(1, len(db.list_events(user["id"])))
        adapter.sync = lambda: _empty_events()
        await Aggregator(db, user["id"], [adapter]).sync()
        self.assertEqual([], db.list_events(user["id"]))


async def _empty_events():
    return []


if __name__ == "__main__":
    unittest.main()
