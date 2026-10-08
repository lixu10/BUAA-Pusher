import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.db import Database
from app.security import CredentialVault
from app.services.notifications import NotificationSender, TriggerDispatcher
from app.services.rules import RuleEngine


class NotificationFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_due_in_app_trigger_reaches_delivery_and_inbox(self):
        db = Database(":memory:")
        db.init()
        user = db.create_user("notify@example.com", "Notify", "hash")
        starts_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        db.upsert_event(user["id"], {
            "source": "custom", "external_id": "custom-1", "kind": "custom",
            "title": "现在发生", "starts_at": starts_at,
        })
        db.create_automation_rule(user["id"], {
            "name": "立即提醒", "enabled": True,
            "scope": {"kinds": ["custom"], "sources": []},
            "trigger": {"anchor": "starts_at", "offset_minutes": 0},
            "conditions": [], "cancel_conditions": [],
            "repeat": {"every_minutes": None, "max_count": 1, "until_anchor": None},
            "actions": [{"channel": "in_app", "channel_id": None, "after_minutes": 0}],
            "quiet_hours_policy": "ignore",
        })
        RuleEngine(db).plan_user(user["id"])
        vault = CredentialVault(Path.cwd(), "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
        dispatcher = TriggerDispatcher(db, NotificationSender(db, vault))
        self.assertEqual(1, await dispatcher.dispatch_due())
        self.assertEqual("sent", db.list_delivery_v2(user["id"])[0]["status"])
        self.assertEqual("现在发生", db.list_inbox(user["id"])[0]["title"])

    async def test_failed_delivery_is_retried_with_backoff(self):
        class FailingSender:
            async def send(self, *_args, **_kwargs):
                raise RuntimeError("gateway unavailable")

        db = Database(":memory:")
        db.init()
        user = db.create_user("retry@example.com", "Retry", "hash")
        starts_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        db.upsert_event(user["id"], {
            "source": "custom", "external_id": "retry-1", "kind": "custom",
            "title": "重试测试", "starts_at": starts_at,
        })
        db.create_automation_rule(user["id"], {
            "name": "重试提醒", "enabled": True,
            "scope": {"kinds": ["custom"], "sources": []},
            "trigger": {"anchor": "starts_at", "offset_minutes": 0},
            "conditions": [], "cancel_conditions": [],
            "repeat": {"every_minutes": None, "max_count": 1, "until_anchor": None},
            "actions": [{"channel": "in_app", "channel_id": None, "after_minutes": 0}],
            "quiet_hours_policy": "ignore",
        })
        RuleEngine(db).plan_user(user["id"])
        self.assertEqual(1, await TriggerDispatcher(db, FailingSender()).dispatch_due())
        trigger = db.list_triggers(user["id"])[0]
        self.assertEqual("queued", trigger["status"])
        self.assertEqual(1, trigger["attempts"])
        self.assertIn("1 分钟后重试", trigger["detail"])


if __name__ == "__main__":
    unittest.main()
