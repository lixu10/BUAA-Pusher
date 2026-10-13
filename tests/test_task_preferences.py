import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.db import Database
from app.models import PreferencesUpdate
from app.services.rules import RuleEngine


class TaskPreferencesTest(unittest.TestCase):
    def test_defaults_partial_updates_tenant_isolation_and_migration(self):
        db = Database(":memory:"); db.init()
        user = db.create_user("task-settings@example.test", "Test", "hash")["id"]
        other = db.create_user("task-other@example.test", "Test", "hash")["id"]
        self.assertEqual(["assignment"], db.preferences(user)["task_visible_kinds"])
        db.update_preferences(user, {"task_visible_kinds": ["assignment", "boya"], "task_visible_sources": ["judge", "bykc"], "quiet_start": "22:00"})
        db.update_preferences(user, {"auth_refresh_hours": 12})
        db.init()
        self.assertEqual(["assignment", "boya"], db.preferences(user)["task_visible_kinds"])
        self.assertEqual(["judge", "bykc"], db.preferences(user)["task_visible_sources"])
        self.assertEqual(["assignment"], db.preferences(other)["task_visible_kinds"])
        self.assertEqual("22:00", db.preferences(user)["quiet_start"])
        db.close()

    def test_validation_and_empty_choices_are_explicit(self):
        self.assertEqual([], PreferencesUpdate(task_visible_kinds=[]).task_visible_kinds)
        self.assertEqual(["assignment"], PreferencesUpdate(task_visible_kinds=["assignment", "assignment"]).task_visible_kinds)
        for data in [{"task_visible_kinds": ["signin"]}, {"task_visible_sources": ["evil"]}, {"task_visible_kinds": None}]:
            with self.assertRaises(ValidationError): PreferencesUpdate(**data)

    def test_api_filters_only_tasks_not_school_events_or_reminder_plans(self):
        import app.main as main
        db = Database(":memory:"); db.init()
        user = db.create_user("task-api@example.test", "Test", "hash")
        event = db.upsert_event(user["id"], {"source":"byxt", "external_id":"lesson", "kind":"course", "title":"lesson", "starts_at":"2030-09-01T10:00:00+08:00", "ends_at":"2030-09-01T11:00:00+08:00"})
        db.update_preferences(user["id"], {"academic_filter_enabled": False})
        db.create_automation_rule(user["id"], {"name":"lesson reminder", "profile":"course", "scope":{"sources":["byxt"],"kinds":["course"]}, "trigger":{"anchor":"starts_at","offset_minutes":-10}, "actions":[{"channel":"in_app"}]})
        engine=RuleEngine(db,True); engine.plan_user(user["id"])
        original=[t["id"] for t in db.list_triggers(user["id"]) if t["status"]=="queued"]
        self.assertTrue(original)
        main.app.dependency_overrides[main.current_user]=lambda:user
        main.app.dependency_overrides[main.csrf_user]=lambda:user
        client=TestClient(main.app)
        try:
            with patch.object(main,"db",db), patch.object(main,"rule_engine",engine):
                result=client.put("/api/preferences",json={"task_visible_kinds":[],"task_visible_sources":[]})
                self.assertEqual(200,result.status_code)
                self.assertEqual([],result.json()["task_visible_kinds"])
                self.assertEqual(1,len(client.get("/api/events").json()))
                self.assertEqual(original,[t["id"] for t in db.list_triggers(user["id"]) if t["status"]=="queued"])
                self.assertEqual(422,client.put("/api/preferences",json={"task_visible_sources":["unknown"]}).status_code)
        finally:
            client.close(); main.app.dependency_overrides.clear(); db.close()
