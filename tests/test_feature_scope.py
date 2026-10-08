import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import app.main as main
from app.db import Database
from app.features import event_available, public_rule, rule_available
from app.services.notifications import TriggerDispatcher
from app.services.rules import RuleEngine


def definition(kind="assignment", source="spoc"):
    return {
        "name": "提醒", "enabled": True,
        "scope": {"kinds": [kind], "sources": [source]},
        "trigger": {"anchor": "starts_at", "offset_minutes": 0},
        "actions": [{"channel": "in_app", "channel_id": None, "after_minutes": 0}],
        "quiet_hours_policy": "ignore",
    }


class FeatureApiTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.init()
        self.user = self.db.create_user("scope@example.com", "Scope", "hash")
        self.uid = self.user["id"]
        self.db.update_preferences(self.uid, {"academic_filter_enabled": False})
        self.client = TestClient(main.app)
        self.db_patch = patch.object(main, "db", self.db)
        self.db_patch.start()
        self.engine_patch = patch.object(main, "rule_engine", RuleEngine(self.db, enforce_feature_scope=True))
        self.engine_patch.start()
        main.app.dependency_overrides[main.current_user] = lambda: self.user
        main.app.dependency_overrides[main.csrf_user] = lambda: self.user

    def tearDown(self):
        main.app.dependency_overrides.clear()
        self.engine_patch.stop()
        self.db_patch.stop()
        self.client.close()

    def test_dashboard_hides_parked_events_and_preserves_database(self):
        for source, kind in [("spoc", "assignment"), ("judge", "assignment"),
                             ("iclass", "signin"), ("byxt", "course"), ("grade", "grade")]:
            self.db.upsert_event(self.uid, {
                "source": source, "external_id": source, "kind": kind,
                "title": source, "starts_at": "2030-10-07T10:00:00+08:00",
            })
        data = self.client.get("/api/dashboard").json()
        self.assertEqual({"spoc", "byxt", "judge"}, {e["source"] for e in data["events"]})
        self.assertEqual(5, len(self.db.list_events(self.uid)))
        active = [s for s in data["sources"] if s["available"]]
        parked = [s for s in data["sources"] if not s["available"]]
        self.assertEqual(3, len(active))
        self.assertEqual(6, len(parked))
        self.assertTrue(all(not s["enabled"] and s["detail"] == "Coming Soon" for s in parked))
        self.assertEqual(3, len(self.client.get("/api/events").json()))

    def test_disabled_features_cannot_be_enabled_via_api(self):
        for source in ["iclass", "grade", "bykc", "libbook", "ygdk", "cgyy", "custom"]:
            response = self.client.patch(f"/api/sources/{source}", json={"enabled": True})
            self.assertEqual(403, response.status_code)
            self.assertEqual("Coming Soon", response.json()["detail"])
        self.assertEqual(403, self.client.post("/api/events", json={"title": "个人日程"}).status_code)
        self.assertEqual(403, self.client.post("/api/rules", json=definition("grade", "grade")).status_code)
        self.assertEqual(201, self.client.post("/api/rules", json=definition()).status_code)
        self.assertEqual(403, self.client.post("/api/sources/iclass/sync").status_code)
        self.assertEqual(201, self.client.post("/api/rules", json=definition("course", "byxt")).status_code)
        parked = self.db.create_automation_rule(self.uid, definition("signin", "iclass"))
        self.assertEqual(403, self.client.patch(f"/api/rules/{parked['id']}", json={"enabled": True}).status_code)
        self.assertEqual(403, self.client.put(f"/api/rules/{parked['id']}", json=definition()).status_code)
        self.assertTrue(self.db.get_automation_rule(self.uid, parked["id"])["enabled"])
        exposed = next(r for r in self.client.get("/api/rules").json() if r["id"] == parked["id"])
        self.assertFalse(exposed["enabled"])
        self.assertFalse(exposed["available"])


class FeatureExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_old_queued_notifications_are_cancelled_before_send(self):
        db = Database(":memory:")
        db.init()
        user = db.create_user("dispatch@example.com", "Dispatch", "hash")
        for source, kind in [("iclass", "signin"), ("spoc", "assignment")]:
            db.upsert_event(user["id"], {
                "source": source, "external_id": source, "kind": kind, "title": source,
                "starts_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            })
            db.create_automation_rule(user["id"], definition(kind, source))
        RuleEngine(db).plan_user(user["id"])
        sender = AsyncMock()
        await TriggerDispatcher(db, sender, enforce_feature_scope=True).dispatch_due()
        sender.send.assert_awaited_once()
        self.assertEqual("spoc", sender.send.call_args.args[3])
        statuses = {t["event_title"]: t["status"] for t in db.list_triggers(user["id"])}
        self.assertEqual({"iclass": "cancelled", "spoc": "sent"}, statuses)

    async def test_planner_does_not_schedule_parked_or_disabled_sources(self):
        db = Database(":memory:")
        db.init()
        user = db.create_user("plan@example.com", "Plan", "hash")
        db.update_preferences(user["id"], {"academic_filter_enabled": False})
        for source in ["spoc", "judge", "custom"]:
            db.upsert_event(user["id"], {
                "source": source, "external_id": source, "kind": "assignment", "title": source,
                "starts_at": "2030-10-07T10:00:00+08:00",
            })
        rule = definition()
        rule["scope"]["sources"] = []
        db.create_automation_rule(user["id"], rule)
        db.upsert_source(user["id"], {"id": "judge", "label": "JUDGE", "status": "healthy"})
        db.set_source_enabled(user["id"], "judge", False)
        RuleEngine(db, enforce_feature_scope=True).plan_user(user["id"])
        self.assertEqual(["spoc"], [t["event_title"] for t in db.list_triggers(user["id"])])


if __name__ == "__main__":
    unittest.main()
