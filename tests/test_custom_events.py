import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import app.main as main
from app.db import Database
from app.services.event_state import public_event
from app.services.notifications import TriggerDispatcher
from app.services.rules import RuleEngine


def payload(remind=True):
    return {"title": "个人会议", "starts_at": "2030-09-02T09:00:00+08:00",
            "ends_at": "2030-09-02T10:00:00+08:00", "location": "线上", "description": "讨论安排",
            "reminder": {"enabled": True, "times": [{"anchor": "starts_at", "offset_minutes": -60},
                                                   {"anchor": "starts_at", "offset_minutes": -10}],
                         "actions": [{"channel": "in_app"}], "quiet_hours_policy": "ignore"} if remind else None}


class CustomEventApiTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.init()
        self.user = self.db.create_user("custom@example.com", "Custom", "hash")
        self.uid = self.user["id"]
        self.client = TestClient(main.app)
        self.db_patch = patch.object(main, "db", self.db)
        self.engine_patch = patch.object(main, "rule_engine", RuleEngine(self.db, enforce_feature_scope=True))
        self.db_patch.start()
        self.engine_patch.start()
        main.app.dependency_overrides[main.current_user] = lambda: self.user
        main.app.dependency_overrides[main.csrf_user] = lambda: self.user

    def tearDown(self):
        main.app.dependency_overrides.clear()
        self.engine_patch.stop()
        self.db_patch.stop()
        self.client.close()
        self.db.close()

    def create(self, values=None):
        response = self.client.post("/api/events", json=values or payload())
        self.assertEqual(201, response.status_code, response.text)
        return response.json()

    def test_create_with_or_without_reminders_is_visible_outside_academic_window(self):
        bare = self.create(payload(False))
        event = self.create()
        self.assertIsNone(bare["reminder"])
        self.assertEqual("custom", event["source"])
        self.assertEqual("未开始", event["status_label"])
        self.assertEqual("讨论安排", event["metadata"]["description"])
        rule = self.db.list_automation_rules(self.uid)[0]
        self.assertEqual([event["id"]], rule["scope"]["event_ids"])
        self.assertEqual(2, len(self.db.list_triggers(self.uid)))
        self.assertEqual({event["id"]}, {row["event_id"] for row in self.db.list_triggers(self.uid)})
        self.assertEqual(2, len(self.client.get("/api/dashboard").json()["events"]))
        self.assertEqual(2, len(self.client.get("/api/events").json()))
        self.assertTrue(self.client.get("/api/rules").json()[0]["available"])
        RuleEngine(self.db, True).plan_user(self.uid)
        self.assertEqual(2, len(self.db.list_triggers(self.uid)))

    def test_edit_reschedules_same_bound_rule_and_remove_reminder_deletes_queue(self):
        event = self.create()
        rule_id = event["reminder"]["rule_id"]
        values = payload()
        values["starts_at"] = "2030-09-03T09:00:00+08:00"
        values["ends_at"] = "2030-09-03T10:00:00+08:00"
        values["reminder"]["times"] = [{"anchor": "ends_at", "offset_minutes": -5}]
        response = self.client.put(f"/api/events/{event['id']}", json=values)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(rule_id, response.json()["reminder"]["rule_id"])
        triggers = self.db.list_triggers(self.uid)
        self.assertEqual(1, len(triggers))
        self.assertEqual(datetime.fromisoformat("2030-09-03T01:55:00+00:00"), datetime.fromisoformat(triggers[0]["run_at"]))
        values["reminder"] = None
        self.assertEqual(200, self.client.put(f"/api/events/{event['id']}", json=values).status_code)
        self.assertEqual([], self.db.list_automation_rules(self.uid))
        self.assertEqual([], self.db.list_triggers(self.uid))
        self.assertIsNone(self.client.get(f"/api/events/{event['id']}").json()["reminder"])

    def test_delete_only_personal_event_and_its_rule(self):
        event = self.create()
        other = self.create(payload(False))
        self.assertEqual(204, self.client.delete(f"/api/events/{event['id']}").status_code)
        self.assertEqual(404, self.client.get(f"/api/events/{event['id']}").status_code)
        self.assertIsNotNone(self.db.event_by_id(self.uid, other["id"]))
        self.assertEqual([], self.db.list_automation_rules(self.uid))
        self.assertEqual([], self.db.list_triggers(self.uid))
        self.db.upsert_event(self.uid, {"source": "byxt", "external_id": "school", "kind": "course", "title": "学校课程"})
        school = self.db.event_by_key(self.uid, "byxt", "school")
        self.assertEqual(404, self.client.delete(f"/api/events/{school['id']}").status_code)
        self.assertEqual(404, self.client.put(f"/api/events/{school['id']}", json=payload()).status_code)
        self.assertIsNotNone(self.db.event_by_id(self.uid, school["id"]))

    def test_tenant_scope_and_channel_ownership(self):
        event = self.create()
        owner = self.uid
        self.user = self.db.create_user("other-custom@example.com", "Other", "hash")
        for method in ("get", "delete"):
            self.assertEqual(404, getattr(self.client, method)(f"/api/events/{event['id']}").status_code)
        self.assertEqual(404, self.client.put(f"/api/events/{event['id']}", json=payload()).status_code)
        self.assertEqual([], self.client.get("/api/events").json())
        channel = self.db.create_channel(owner, "email", "private", "ciphertext", True)
        values = payload()
        values["reminder"]["actions"] = [{"channel": "email", "channel_id": channel["id"]}]
        self.assertEqual(422, self.client.post("/api/events", json=values).status_code)
        self.assertEqual([], self.db.list_events(self.user["id"]))

    def test_invalid_payloads_do_not_create_partial_events_or_rules(self):
        cases = [{"title": "   "}, {"starts_at": "not-a-date"}, {"starts_at": "2030-09-02T09:00:00"},
                 {"ends_at": "2030-09-01T09:00:00+08:00"}, {"kind": "course"}, {"metadata": {"secret": "bad"}}]
        for change in cases:
            self.assertEqual(422, self.client.post("/api/events", json={**payload(), **change}).status_code)
        for points in ([], [{"anchor": "ends_at", "offset_minutes": 1}],
                       [{"anchor": "starts_at", "offset_minutes": -1.5}],
                       [{"anchor": "starts_at", "offset_minutes": -1}] * 2,
                       [{"anchor": "starts_at", "offset_minutes": -60}, {"anchor": "ends_at", "offset_minutes": -120}]):
            values = payload()
            values["reminder"]["times"] = points
            self.assertEqual(422, self.client.post("/api/events", json=values).status_code)
        values = payload()
        values["ends_at"] = None
        values["reminder"]["times"] = [{"anchor": "ends_at", "offset_minutes": -5}]
        self.assertEqual(422, self.client.post("/api/events", json=values).status_code)
        self.assertEqual([], self.db.list_events(self.uid))
        self.assertEqual([], self.db.list_automation_rules(self.uid))

    def test_disabled_rule_can_be_toggled_but_not_retargeted_through_general_api(self):
        event = self.create()
        rule_id = event["reminder"]["rule_id"]
        self.assertEqual(200, self.client.patch(f"/api/rules/{rule_id}", json={"enabled": False}).status_code)
        self.assertFalse(self.client.get(f"/api/events/{event['id']}").json()["reminder"]["enabled"])
        self.assertTrue(all(row["status"] == "cancelled" for row in self.db.list_triggers(self.uid)))
        self.assertEqual(422, self.client.patch(f"/api/rules/{rule_id}", json={"scope": {"kinds": ["custom"], "sources": ["custom"], "event_ids": [999]}}).status_code)
        self.assertEqual(200, self.client.patch(f"/api/rules/{rule_id}", json={"enabled": True}).status_code)
        self.assertEqual(2, len([row for row in self.db.list_triggers(self.uid) if row["status"] == "queued"]))

    def test_event_and_rule_database_writes_are_atomic(self):
        values = {"title": "atomic", "starts_at": "2030-09-02T09:00:00+08:00", "metadata": {}}
        reminder = {"enabled": True, "times": [{"anchor": "starts_at", "offset_minutes": -5}],
                    "actions": [{"channel": "in_app", "not_serializable": object()}], "quiet_hours_policy": "ignore"}
        with self.assertRaises(TypeError):
            self.db.save_custom_event(self.uid, values, reminder)
        self.assertEqual([], self.db.list_events(self.uid))
        self.assertEqual([], self.db.list_automation_rules(self.uid))

    def test_custom_task_preferences_are_supported_without_changing_previous_choices(self):
        response = self.client.put("/api/preferences", json={"task_visible_kinds": ["assignment", "custom"], "task_visible_sources": ["spoc", "judge", "custom"]})
        self.assertEqual(200, response.status_code)
        self.assertIn("custom", response.json()["task_visible_kinds"])


class CustomEventDispatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_local_reminder_delivers_without_school_sync_and_does_not_repeat(self):
        db = Database(":memory:")
        db.init()
        uid = db.create_user("local@example.com", "Local", "hash")["id"]
        now = datetime.now(UTC)
        event = db.save_custom_event(uid, {"title": "会面", "starts_at": (now + timedelta(minutes=10)).isoformat(),
                                          "ends_at": (now + timedelta(hours=1)).isoformat(), "location": "线上", "metadata": {"description": "准备资料"}},
                                     {"enabled": True, "times": [{"anchor": "starts_at", "offset_minutes": -10}],
                                      "actions": [{"channel": "in_app", "after_minutes": 0}], "quiet_hours_policy": "ignore"})
        engine = RuleEngine(db, True)
        engine.plan_user(uid, now)
        sender, refresher = AsyncMock(), AsyncMock(side_effect=AssertionError("no school API allowed"))
        dispatcher = TriggerDispatcher(db, sender, True, source_refresher=refresher)
        self.assertEqual(1, await dispatcher.dispatch_due())
        sender.send.assert_awaited_once()
        refresher.assert_not_awaited()
        body = sender.send.call_args.args[4]
        for text in ("自定义日程提醒", "个人日程", "准备资料", "线上", "开始", "结束"):
            self.assertIn(text, body)
        engine.plan_user(uid, now)
        self.assertEqual(0, await dispatcher.dispatch_due())
        self.assertEqual("sent", db.list_triggers(uid)[0]["status"])
        self.assertEqual("未开始", public_event(event)["status_label"])
        db.close()

    async def test_end_time_reminder_has_poll_grace_and_completed_or_ignored_events_cancel(self):
        db = Database(":memory:")
        db.init()
        uid = db.create_user("end@example.com", "End", "hash")["id"]
        now = datetime.now(UTC)
        reminder = {"enabled": True, "times": [{"anchor": "ends_at", "offset_minutes": 0}],
                    "actions": [{"channel": "in_app", "after_minutes": 0}], "quiet_hours_policy": "ignore"}
        event = db.save_custom_event(uid, {"title": "结束时", "starts_at": (now - timedelta(hours=1)).isoformat(), "ends_at": (now - timedelta(seconds=1)).isoformat()}, reminder)
        RuleEngine(db, True).plan_user(uid, now)
        sender = AsyncMock()
        self.assertEqual(1, await TriggerDispatcher(db, sender, True).dispatch_due())
        sender.send.assert_awaited_once()
        for status, ignored in (("done", False), ("pending", True)):
            db.upsert_event(uid, {**event, "external_id": f"skip-{status}", "status": status})
            skipped = db.event_by_key(uid, "custom", f"skip-{status}")
            rule = db.create_automation_rule(uid, {"name": "skip", "enabled": True, "profile": "custom-event", "scope": {"sources": ["custom"], "kinds": ["custom"], "event_ids": [skipped["id"]]}, "triggers": reminder["times"], "actions": reminder["actions"]})
            if ignored:
                db.set_event_ignored(uid, skipped["id"], True)
            RuleEngine(db, True).plan_user(uid, now)
            self.assertEqual([], [row for row in db.list_triggers(uid) if row["rule_id"] == rule["id"] and row["status"] == "queued"])
        db.close()


if __name__ == "__main__":
    unittest.main()
