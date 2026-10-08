import unittest

from app.db import Database
from app.services.rules import RULE_TEMPLATES, RuleEngine


class RuleEngineTest(unittest.TestCase):
    def test_public_template_id_never_replaces_database_rule_id(self):
        db = Database(":memory:")
        db.init()
        user = db.create_user("template@example.com", "Template", "hash")
        rule = db.create_automation_rule(user["id"], {
            "id": "grade-published", "name": "新成绩提醒", "enabled": True,
            "scope": {"kinds": ["grade"], "sources": ["grade"]},
            "trigger": {"anchor": "starts_at", "offset_minutes": 0},
            "conditions": [], "cancel_conditions": [],
            "repeat": {"every_minutes": None, "max_count": 1, "until_anchor": None},
            "actions": [{"channel": "in_app", "channel_id": None, "after_minutes": 0}],
            "quiet_hours_policy": "delay",
        })
        self.assertIsInstance(rule["id"], int)

    def test_exam_template_has_three_stages(self):
        template = next(item for item in RULE_TEMPLATES if item["id"] == "exam-start")
        self.assertEqual([-10080, -1440, -120], [
            template["trigger"]["offset_minutes"] + action["after_minutes"]
            for action in template["actions"]
        ])

    def test_schedules_once_within_tenant(self):
        db = Database(":memory:")
        db.init()
        user = db.create_user("one@example.com", "One", "hash")
        other = db.create_user("two@example.com", "Two", "hash")
        event_time = "2030-09-19T10:00:00+08:00"
        db.upsert_event(user["id"], {
            "source": "test", "external_id": "course-1", "kind": "course",
            "title": "测试课程", "starts_at": event_time,
        })
        db.create_automation_rule(user["id"], {
            "name": "上课提醒", "enabled": True,
            "scope": {"kinds": ["course"], "sources": []},
            "trigger": {"anchor": "starts_at", "offset_minutes": -15},
            "conditions": [], "cancel_conditions": [],
            "repeat": {"every_minutes": None, "max_count": 1, "until_anchor": None},
            "actions": [{"channel": "in_app", "channel_id": None, "after_minutes": 0}],
            "quiet_hours_policy": "ignore",
        })
        engine = RuleEngine(db)
        self.assertEqual(1, engine.plan_user(user["id"]))
        self.assertEqual(0, engine.plan_user(user["id"]))
        self.assertEqual(1, len(db.list_triggers(user["id"])))
        self.assertEqual([], db.list_events(other["id"]))

    def test_condition_operators(self):
        event = {"status": "missing", "metadata": {"signed": False}}
        self.assertTrue(RuleEngine.conditions_match([
            {"field": "status", "op": "eq", "value": "missing"},
            {"field": "metadata.signed", "op": "eq", "value": False},
        ], event))
        self.assertTrue(RuleEngine.conditions_match([
            {"field": "metadata.remaining", "op": "gt", "value": 2},
            {"field": "metadata.remaining", "op": "lte", "value": "3"},
            {"field": "title", "op": "contains", "value": "作业"},
        ], {"title": "第一次作业", "metadata": {"remaining": 3}}))

    def test_replanning_replaces_obsolete_queued_trigger(self):
        db = Database(":memory:")
        db.init()
        user = db.create_user("plan@example.com", "Plan", "hash")
        db.upsert_event(user["id"], {
            "source": "test", "external_id": "exam-1", "kind": "exam",
            "title": "测试考试", "starts_at": "2030-09-19T10:00:00+08:00",
        })
        rule = db.create_automation_rule(user["id"], {
            "name": "考试提醒", "enabled": True,
            "scope": {"kinds": ["exam"], "sources": []},
            "trigger": {"anchor": "starts_at", "offset_minutes": -15},
            "conditions": [], "cancel_conditions": [],
            "repeat": {"every_minutes": None, "max_count": 1, "until_anchor": None},
            "actions": [{"channel": "in_app", "channel_id": None, "after_minutes": 0}],
            "quiet_hours_policy": "ignore",
        })
        engine = RuleEngine(db)
        engine.plan_user(user["id"])
        first = db.list_triggers(user["id"])
        self.assertEqual(1, len(first))
        db.update_automation_rule(user["id"], rule["id"], {
            "trigger": {"anchor": "starts_at", "offset_minutes": -30},
        })
        engine.plan_user(user["id"])
        second = db.list_triggers(user["id"])
        self.assertEqual(1, len(second))
        self.assertNotEqual(first[0]["run_at"], second[0]["run_at"])

    def test_multiple_business_triggers_plan_independently(self):
        db = Database(":memory:")
        db.init()
        user = db.create_user("booking@example.com", "Booking", "hash")
        db.upsert_event(user["id"], {
            "source": "libbook", "external_id": "booking-1", "kind": "booking",
            "title": "图书馆预约", "starts_at": "2030-09-19T10:00:00+08:00",
            "ends_at": "2030-09-19T12:00:00+08:00", "status": "upcoming",
        })
        db.create_automation_rule(user["id"], {
            "name": "图书馆预约提醒", "enabled": True,
            "scope": {"kinds": ["booking"], "sources": ["libbook"]},
            "trigger": {"anchor": "starts_at", "offset_minutes": -30},
            "triggers": [
                {"anchor": "starts_at", "offset_minutes": -30},
                {"anchor": "ends_at", "offset_minutes": -15},
            ],
            "conditions": [], "cancel_conditions": [],
            "repeat": {"every_minutes": None, "max_count": 1, "until_anchor": None},
            "actions": [{"channel": "in_app", "channel_id": None, "after_minutes": 0}],
            "quiet_hours_policy": "ignore", "profile": "library",
            "profile_settings": {"before_start": 30, "before_expiry": 15},
        })
        RuleEngine(db).plan_user(user["id"])
        triggers = db.list_triggers(user["id"])
        self.assertEqual(2, len(triggers))
        self.assertNotEqual(triggers[0]["run_at"], triggers[1]["run_at"])


if __name__ == "__main__":
    unittest.main()
