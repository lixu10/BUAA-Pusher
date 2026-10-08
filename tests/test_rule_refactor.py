import copy
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main as main
from app.adapters.spoc import SpocAdapter, DETAIL_URL, SUBMISSION_URL, CURRENT_TERM_URL, ASSIGNMENTS_URL
from app.db import Database
from app.features import rule_available
from app.models import AutomationRuleCreate
from app.security import CredentialVault
from app.services.aggregator import Aggregator
from app.services.notifications import NotificationSender, TriggerDispatcher
from app.services.rules import RuleEngine
from app.services.sync_changes import SyncChangeTracker


def assignment(key="a1", source="spoc", **metadata):
    return {"external_id": key, "source": source, "kind": "assignment", "title": "课程 · 作业",
            "due_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(), "status": "pending",
            "metadata": {"grading_known": True, "graded": False, **metadata}}


def course(key="c1", **overrides):
    day = (datetime.now(UTC) + timedelta(days=3)).date().isoformat()
    return {"external_id": key, "source": "byxt", "kind": "course", "title": "课程",
            "starts_at": f"{day}T08:00:00+08:00", "ends_at": f"{day}T09:35:00+08:00",
            "location": "A101", "metadata": {"courseCode": "code", "courseSerialNo": "1",
                "term_code": "term", "week_start": "week", "teacher": "教师甲"}, **overrides}


def definition(profile, **settings):
    source = "byxt" if profile.startswith("course") else "judge" if profile.startswith("judge") else "spoc"
    return {"name": profile, "profile": profile, "enabled": True,
            "profile_settings": settings,
            "scope": {"sources": [source], "kinds": ["course" if source == "byxt" else "assignment"]},
            "actions": [{"channel": "in_app", "channel_id": None, "after_minutes": 0}],
            "quiet_hours_policy": "ignore"}


class RefactorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.init()
        self.user = self.db.create_user("refactor@example.com", "Test", "hash")
        self.uid = self.user["id"]
        self.db.update_preferences(self.uid, {"academic_filter_enabled": False})
        self.engine = RuleEngine(self.db, enforce_feature_scope=True)
        self.tracker = SyncChangeTracker(self.db, self.uid)
        vault = CredentialVault(Path.cwd(), "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
        self.dispatcher = TriggerDispatcher(self.db, NotificationSender(self.db, vault), enforce_feature_scope=True)

    def tearDown(self):
        self.db.close()

    def record(self, source, events, complete=True):
        for event in events:
            self.db.upsert_event(self.uid, event)
        saved = [self.db.event_by_key(self.uid, source, event["external_id"]) for event in events]
        self.tracker.record(source, saved, complete)
        if complete:
            self.db.remove_missing_source_events(self.uid, source, {e["external_id"] for e in events})
        self.engine.plan_user(self.uid)

    def add_rule(self, profile, **settings):
        return self.db.create_automation_rule(self.uid, AutomationRuleCreate.model_validate(
            definition(profile, **settings)).model_dump())

    async def test_first_snapshot_silent_then_new_assignment_once_across_replan(self):
        self.add_rule("spoc-new-assignment")
        first = assignment()
        self.record("spoc", [first])
        self.assertEqual([], self.db.list_sync_changes(self.uid))
        second = assignment("a2")
        self.record("spoc", [first, second])
        self.assertEqual(1, len(self.db.list_triggers(self.uid)))
        self.record("spoc", [first, second])
        self.assertEqual(1, len(self.db.list_sync_changes(self.uid)))
        await self.dispatcher.dispatch_due()
        self.engine.plan_user(self.uid)
        await self.dispatcher.dispatch_due()
        self.assertEqual(1, len(self.db.list_inbox(self.uid)))
        self.assertIsNotNone(self.db.list_delivery_v2(self.uid)[0]["change_trigger_id"])

    async def test_cancelled_course_notification_survives_event_removal(self):
        self.add_rule("course-change", change_types=["cancelled"])
        self.record("byxt", [course()])
        self.record("byxt", [])
        self.assertEqual([], self.db.list_events(self.uid))
        self.assertEqual(1, await self.dispatcher.dispatch_due())
        self.assertIn("课程取消", self.db.list_inbox(self.uid)[0]["body"])

    async def test_course_move_is_one_time_change_not_add_and_cancel(self):
        self.add_rule("course-change", change_types=["time"])
        first = course()
        later = copy.deepcopy(first)
        later.update(external_id="moved", starts_at=first["starts_at"].replace("08:00", "14:00"),
                     ends_at=first["ends_at"].replace("09:35", "15:35"))
        self.record("byxt", [first])
        self.record("byxt", [later])
        changes = self.db.list_sync_changes(self.uid)
        self.assertEqual(1, len(changes))
        self.assertEqual(["time"], changes[0]["payload"]["categories"])
        await self.dispatcher.dispatch_due()
        body = self.db.list_inbox(self.uid)[0]["body"]
        self.assertIn("原时间", body)
        self.assertIn("现时间", body)

    async def test_different_course_categories_have_independent_rules(self):
        self.add_rule("course-change", change_types=["location"])
        self.add_rule("course-change", change_types=["teacher"])
        self.record("byxt", [course()])
        changed = course(location="B202")
        self.record("byxt", [changed])
        self.assertEqual(1, len(self.db.list_triggers(self.uid)))
        await self.dispatcher.dispatch_due()
        self.assertIn("A101 → B202", self.db.list_inbox(self.uid)[0]["body"])

    async def test_added_course_is_part_of_course_change(self):
        self.add_rule("course-change", change_types=["added"])
        self.record("byxt", [])
        self.record("byxt", [course()])
        await self.dispatcher.dispatch_due()
        self.assertIn("新增课程", self.db.list_inbox(self.uid)[0]["body"])

    def test_partial_snapshot_never_cancels_or_establishes_baseline(self):
        self.add_rule("course-change", change_types=["cancelled", "added"])
        self.record("byxt", [], complete=False)
        self.assertIsNone(self.db.get_sync_snapshot(self.uid, "byxt"))
        self.record("byxt", [course()])
        self.record("byxt", [], complete=False)
        self.assertEqual([], self.db.list_sync_changes(self.uid))
        self.assertEqual(1, len(self.db.list_events(self.uid)))

    async def test_zero_score_and_comment_update_after_deadline_are_not_suppressed(self):
        self.add_rule("spoc-assignment-grade")
        first = assignment()
        first["due_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        first["status"] = "done"
        self.record("spoc", [first])
        graded = copy.deepcopy(first)
        graded["metadata"].update(graded=True, earned_score=0, grade_comment="请修改", max_score=100)
        self.record("spoc", [graded])
        await self.dispatcher.dispatch_due()
        self.assertIn("得分：0 / 100", self.db.list_inbox(self.uid)[0]["body"])
        graded["metadata"]["grade_comment"] = "已复核"
        self.record("spoc", [graded])
        await self.dispatcher.dispatch_due()
        self.assertEqual(2, len(self.db.list_inbox(self.uid)))
        self.assertIn("评分更新", self.db.list_inbox(self.uid)[0]["body"])

    def test_unknown_grade_and_first_grade_snapshot_do_not_notify(self):
        self.add_rule("spoc-assignment-grade")
        first = assignment(grading_known=False)
        self.record("spoc", [first])
        graded = assignment(graded=True, earned_score=95)
        self.record("spoc", [graded])
        self.assertEqual([], self.db.list_sync_changes(self.uid))
        self.record("spoc", [assignment(grading_known=False)])
        self.record("spoc", [graded])
        self.assertEqual([], self.db.list_sync_changes(self.uid))

    def test_max_score_or_submission_change_is_not_grading(self):
        self.add_rule("spoc-assignment-grade")
        self.record("spoc", [assignment(max_score=100)])
        second = assignment(max_score=120)
        second["status"] = "done"
        self.record("spoc", [second])
        self.assertEqual([], self.db.list_sync_changes(self.uid))

    async def test_ignored_move_stays_ignored_and_does_not_notify(self):
        self.add_rule("course-change", change_types=["time"])
        self.record("byxt", [course()])
        saved = self.db.event_by_key(self.uid, "byxt", "c1")
        self.db.set_event_ignored(self.uid, saved["id"], True)
        moved = course("c2")
        moved["starts_at"] = moved["starts_at"].replace("08:00", "10:00")
        self.record("byxt", [moved])
        self.assertTrue(self.db.event_by_key(self.uid, "byxt", "c2")["ignored"])
        self.assertEqual(0, await self.dispatcher.dispatch_due())

    async def test_ignore_after_detection_cancels_change_delivery(self):
        self.add_rule("spoc-new-assignment")
        self.record("spoc", [])
        self.record("spoc", [assignment()])
        event = self.db.event_by_key(self.uid, "spoc", "a1")
        self.db.set_event_ignored(self.uid, event["id"], True)
        self.assertEqual(0, await self.dispatcher.dispatch_due())
        self.assertEqual("cancelled", self.db.list_triggers(self.uid)[0]["status"])

    def test_rules_created_or_enabled_after_change_do_not_replay_history(self):
        self.record("spoc", [])
        self.record("spoc", [assignment()])
        self.add_rule("spoc-new-assignment")
        self.engine.plan_user(self.uid)
        self.assertEqual([], self.db.list_triggers(self.uid))

    def test_changed_rules_do_not_replay_old_change_with_new_actions(self):
        rule = self.add_rule("spoc-new-assignment")
        self.record("spoc", [])
        self.record("spoc", [assignment()])
        self.db.update_automation_rule(self.uid, rule["id"], {"actions": [
            {"channel": "in_app", "after_minutes": 0}, {"channel": "email", "after_minutes": 0}]})
        self.engine.plan_user(self.uid)
        self.assertEqual(1, len(self.db.list_triggers(self.uid)))
        self.assertEqual("cancelled", self.db.list_triggers(self.uid)[0]["status"])

    async def test_change_queue_retry_is_durable_and_not_reset_by_replan(self):
        self.add_rule("judge-new-assignment")
        self.record("judge", [])
        self.record("judge", [assignment(source="judge")])
        sender = AsyncMock()
        sender.send.side_effect = RuntimeError("network")
        await TriggerDispatcher(self.db, sender, enforce_feature_scope=True).dispatch_due()
        before = self.db.list_triggers(self.uid)[0]
        self.engine.plan_user(self.uid)
        after = self.db.list_triggers(self.uid)[0]
        self.assertEqual(1, after["attempts"])
        self.assertEqual(before["run_at"], after["run_at"])

    async def test_change_tenants_and_disabled_source(self):
        self.add_rule("spoc-new-assignment")
        self.record("spoc", [])
        self.record("spoc", [assignment()])
        other = self.db.create_user("other@example.com", "Other", "hash")
        self.assertEqual([], self.db.list_sync_changes(other["id"]))
        self.db.upsert_source(self.uid, {"id":"spoc", "label":"SPOC", "status":"healthy"})
        self.db.set_source_enabled(self.uid, "spoc", False)
        self.assertEqual(0, await self.dispatcher.dispatch_due())
        self.assertEqual([], self.db.list_inbox(self.uid))

    async def test_quiet_delay_cannot_send_deadline_alert_at_deadline(self):
        point = [{"days":0,"hours":0,"minutes":1}]
        rule = definition("spoc-assignment", reminder_times=point)
        rule["trigger"] = {"anchor":"due_at", "offset_minutes":-1}
        self.db.create_automation_rule(self.uid, AutomationRuleCreate.model_validate(rule).model_dump())
        event = assignment()
        event["due_at"] = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        self.record("spoc", [event])
        with patch.object(self.dispatcher, "_quiet_decision", return_value=datetime.now(UTC)+timedelta(hours=1)):
            await self.dispatcher.dispatch_due()
        self.assertEqual("skipped", self.db.list_triggers(self.uid)[0]["status"])
        self.assertEqual([], self.db.list_inbox(self.uid))

    def test_academic_filter_applies_to_changes(self):
        self.add_rule("spoc-new-assignment")
        self.db.update_preferences(self.uid, {"academic_filter_enabled": True, "academic_start_date":"2020-01-01", "academic_end_date":"2020-02-01"})
        self.record("spoc", [])
        self.record("spoc", [assignment()])
        self.assertEqual([], self.db.list_triggers(self.uid))


class TimingValidationTest(unittest.TestCase):
    def test_course_end_and_start_use_separate_anchors(self):
        for timing, anchor in [("before_start", "starts_at"), ("before_end", "ends_at")]:
            payload = definition("course", timing=timing, advance_minutes=20)
            model = AutomationRuleCreate.model_validate(payload)
            self.assertEqual(anchor, model.trigger.anchor)
            self.assertEqual(-20, model.trigger.offset_minutes)
        for settings in [{"timing":"due","advance_minutes":20}, {"advance_minutes":-1}, {"advance_minutes":1.5}]:
            with self.assertRaises(ValidationError):
                AutomationRuleCreate.model_validate(definition("course", **settings))
        db = Database(":memory:")
        db.init()
        user = db.create_user("course-end@example.com", "Test", "hash")
        db.upsert_event(user["id"], course())
        rule = AutomationRuleCreate.model_validate(definition("course", timing="before_end", advance_minutes=20))
        db.create_automation_rule(user["id"], rule.model_dump())
        RuleEngine(db).plan_user(user["id"])
        expected = RuleEngine.parse_time(course()["ends_at"]) - timedelta(minutes=20)
        self.assertEqual(expected.isoformat(), db.list_triggers(user["id"])[0]["run_at"])
        db.close()

    def payload(self, points):
        result = definition("spoc-assignment", reminder_times=points)
        result["trigger"] = {"anchor":"due_at", "offset_minutes":-120}
        return result

    def test_mixed_days_hours_minutes_are_summed_not_independent_alerts(self):
        model = AutomationRuleCreate.model_validate(self.payload([
            {"days":0,"hours":2,"minutes":30}, {"days":1,"hours":1,"minutes":5}]))
        self.assertEqual([-150, -1505], [t.offset_minutes for t in model.triggers])
        self.assertEqual(1, model.repeat.max_count)
        direct = definition("spoc-assignment", reminder_times=[{"minutes":1}])
        self.assertEqual(-1, AutomationRuleCreate.model_validate(direct).trigger.offset_minutes)
        with self.assertRaises(ValidationError):
            AutomationRuleCreate.model_validate(definition("spoc-assignment", reminder_times=None))

    def test_invalid_timing_and_duplicate_points_rejected(self):
        for points in [[{"days":0,"hours":0,"minutes":0}], [{"hours":24}], [{"minutes":60}],
                       [{"minutes":1.5}], [{"minutes":True}], [{"minutes":-1}], [],
                       [{"minutes":30}, {"minutes":30}], [{"minutes":1}]*11]:
            with self.subTest(points=points), self.assertRaises(ValidationError):
                AutomationRuleCreate.model_validate(self.payload(points))

    def test_connector_types_are_not_interchangeable(self):
        for profile in ["course-change", "spoc-new-assignment", "judge-new-assignment", "spoc-assignment-grade"]:
            value = definition(profile, change_types=["added"])
            self.assertTrue(rule_available(value))
            value["scope"]["sources"] = ["iclass"]
            self.assertFalse(rule_available(value))
        with self.assertRaises(ValidationError):
            AutomationRuleCreate.model_validate(definition("course-change", change_types=[]))


class SpocGradeTest(unittest.IsolatedAsyncioTestCase):
    def test_personal_grade_not_maximum_and_zero_is_valid(self):
        detail = {"zyfs":100, "pf":70, "pyfs":"2"}
        value = SpocAdapter.grading(detail, {"pf":0,"py":"<p>请修改 &amp; 重交</p>","pyfs":"2"})
        self.assertTrue(value["graded"])
        self.assertEqual(0, value["earned_score"])
        self.assertEqual("请修改 & 重交", value["grade_comment"])
        self.assertEqual(100, value["max_score"])

    def test_peer_grading_and_missing_state_do_not_claim_teacher_grade(self):
        self.assertFalse(SpocAdapter.grading({"zyfs":100}, {"pf":80,"pyfs":"3"})["graded"])
        self.assertFalse(SpocAdapter.grading({"zyfs":100}, {"pf":80})["grading_known"])
        self.assertTrue(SpocAdapter.grading({"zyfs":100}, None)["grading_known"])

    async def test_adapter_reads_grade_from_existing_read_only_routes(self):
        adapter = SpocAdapter(AsyncMock())
        adapter._post = AsyncMock(side_effect=[{"mrxq":"term"}, {"list":[{"zyid":"1","zymc":"任务","tjzt":"1"}]}])
        adapter._get = AsyncMock(return_value=[])
        adapter._get_object = AsyncMock(side_effect=[{"zyfs":100,"pyfs":"2"}, {"pf":0,"py":"评语","pyfs":"2","tjzt":"1"}])
        events = await adapter._sync_authenticated()
        self.assertEqual([DETAIL_URL, SUBMISSION_URL], [call.args[0] for call in adapter._get_object.call_args_list])
        self.assertEqual([CURRENT_TERM_URL, ASSIGNMENTS_URL], [call.args[0] for call in adapter._post.call_args_list])
        self.assertTrue(events[0]["metadata"]["graded"])
        self.assertEqual(0, events[0]["metadata"]["earned_score"])
        self.assertNotIn("score", events[0]["metadata"])


class RuleRefactorApiTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.init()
        self.user = self.db.create_user("api@example.com", "API", "hash")
        self.client = TestClient(main.app)
        self.dbpatch = patch.object(main, "db", self.db)
        self.enginepatch = patch.object(main, "rule_engine", RuleEngine(self.db, enforce_feature_scope=True))
        self.dbpatch.start()
        self.enginepatch.start()
        main.app.dependency_overrides[main.csrf_user] = lambda: self.user

    def tearDown(self):
        main.app.dependency_overrides.clear()
        self.enginepatch.stop()
        self.dbpatch.stop()
        self.client.close()
        self.db.close()

    def test_create_all_connector_specific_types_and_validate_patch(self):
        course_rule = self.client.post("/api/rules", json=definition("course", timing="before_end", advance_minutes=25))
        self.assertEqual(201, course_rule.status_code, course_rule.text)
        self.assertEqual({"anchor":"ends_at","offset_minutes":-25}, course_rule.json()["trigger"])
        for profile in ["course-change", "spoc-new-assignment", "judge-new-assignment", "spoc-assignment-grade"]:
            response = self.client.post("/api/rules", json=definition(profile, change_types=["time","added"]))
            self.assertEqual(201, response.status_code, response.text)
        value = TimingValidationTest().payload([{"hours":2,"minutes":30}])
        created = self.client.post("/api/rules", json=value)
        self.assertEqual(201, created.status_code, created.text)
        self.assertEqual(-150, created.json()["triggers"][0]["offset_minutes"])
        invalid = self.client.patch(f"/api/rules/{created.json()['id']}", json={"profile_settings":{"reminder_times":[{"minutes":0}]}})
        self.assertEqual(422, invalid.status_code)
