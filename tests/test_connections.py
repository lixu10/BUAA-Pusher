import unittest

from app.db import Database
from app.services.rules import RuleEngine


class SchoolConnectionTest(unittest.TestCase):
    def test_status_update_preserves_encrypted_password(self):
        db = Database(":memory:")
        db.init()
        user = db.create_user("school@example.com", "School", "hash")
        db.upsert_school_connection(user["id"], {
            "school_id": "12345678",
            "status": "connected",
            "remember_password": True,
            "password_ciphertext": "encrypted-value",
        })
        db.upsert_school_connection(user["id"], {"status": "degraded", "detail": "route changed"})
        secret = db.school_secret(user["id"])
        self.assertEqual("encrypted-value", secret["password_ciphertext"])
        self.assertNotIn("password_ciphertext", db.school_connection(user["id"]))

    def test_disabling_source_cancels_queued_triggers(self):
        db = Database(":memory:")
        db.init()
        user = db.create_user("source@example.com", "Source", "hash")
        db.upsert_source(user["id"], {
            "id": "spoc", "label": "SPOC", "status": "healthy", "event_count": 1,
        })
        db.upsert_event(user["id"], {
            "source": "spoc", "external_id": "a1", "kind": "assignment",
            "title": "作业", "due_at": "2030-09-19T23:59:00+08:00",
        })
        db.create_automation_rule(user["id"], {
            "name": "作业提醒", "enabled": True,
            "scope": {"kinds": ["assignment"], "sources": ["spoc"]},
            "trigger": {"anchor": "due_at", "offset_minutes": -60},
            "conditions": [], "cancel_conditions": [],
            "repeat": {"every_minutes": None, "max_count": 1, "until_anchor": None},
            "actions": [{"channel": "in_app", "channel_id": None, "after_minutes": 0}],
            "quiet_hours_policy": "ignore",
        })
        RuleEngine(db).plan_user(user["id"])
        source = db.set_source_enabled(user["id"], "spoc", False)
        self.assertFalse(source["enabled"])
        self.assertEqual("cancelled", db.list_triggers(user["id"])[0]["status"])


if __name__ == "__main__":
    unittest.main()
