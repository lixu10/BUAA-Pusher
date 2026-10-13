import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from app.db import Database
from app.models import AutomationRuleCreate
from app.services.attendance import AttendanceService, cached_status, merge_calendar, slot
from app.services.notifications import TriggerDispatcher
from app.services.rules import RuleEngine


def rule_payload(points=None, actions=1, excluded=None):
    return dict(name="未签到检查", enabled=True, profile="signin",
        scope={"kinds":["signin"], "sources":["iclass"]},
        profile_settings={"check_times": points or [{"anchor":"starts_at","offset_minutes":0}],
                          "excluded_courses":excluded or []},
        actions=[{"channel":"in_app"} for _ in range(actions)], quiet_hours_policy="ignore")


class AttendanceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database(":memory:"); self.db.init()
        self.uid = self.db.create_user("attendance@example.test", "Test", "hash")["id"]
        self.other = self.db.create_user("other@example.test", "Other", "hash")["id"]
        self.db.upsert_source(self.uid, {"id":"iclass","label":"课程签到","status":"healthy"})
        self.db.update_source_settings(self.uid, "iclass", enabled=True)
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.raw = dict(source="byxt", external_id="course", kind="course", title="测试课程",
            starts_at=self.now.isoformat(), ends_at=(self.now+timedelta(minutes=60)).isoformat(),
            location="教室", metadata={"course_name":"测试课程", "teacher":"教师"})
        self.db.upsert_event(self.uid, self.raw)
        self.course = self.db.event_by_key(self.uid, "byxt", "course")
        self.query = AsyncMock(); self.sender = AsyncMock()
        self.service = AttendanceService(self.db, self.query)
        self.engine = RuleEngine(self.db, enforce_feature_scope=True)
        self.dispatcher = TriggerDispatcher(self.db, self.sender, True, self.service.prepare)

    def record(self, status="missing", **values):
        return {**self.raw, "source":"iclass", "kind":"signin", "external_id":"attendance",
                "status":status, **values}

    def create(self, points=None, actions=1, excluded=None):
        return self.db.create_automation_rule(self.uid,
            AutomationRuleCreate.model_validate(rule_payload(points, actions, excluded)).model_dump())

    async def test_multiple_channels_query_once_and_keep_course_business_status(self):
        self.create(actions=2); self.engine.plan_user(self.uid, self.now)
        self.query.return_value = [self.record()]
        await self.dispatcher.dispatch_due()
        self.query.assert_awaited_once(); self.assertEqual(2, self.sender.send.await_count)
        self.assertEqual("missing", cached_status(self.db.event_by_id(self.uid, self.course["id"])))
        self.assertEqual("pending", self.db.event_by_id(self.uid, self.course["id"])["status"])
        self.assertEqual([], self.db.list_events(self.other))
        self.assertIn("未签到", self.sender.send.await_args.args[3])

    async def test_signed_at_checkpoint_cancels_remaining_points_and_channels(self):
        self.create([{"anchor":"starts_at","offset_minutes":0}, {"anchor":"ends_at","offset_minutes":-5}], 2)
        self.engine.plan_user(self.uid, self.now); self.query.return_value = [self.record("done")]
        await self.dispatcher.dispatch_due()
        self.query.assert_awaited_once(); self.sender.send.assert_not_awaited()
        self.assertTrue(all(t["status"] == "cancelled" for t in self.db.list_triggers(self.uid)))

    async def test_previously_synced_signed_never_queries(self):
        self.create(); self.engine.plan_user(self.uid, self.now)
        self.service.record(self.uid, [self.record("done")])
        await self.dispatcher.dispatch_due()
        self.query.assert_not_awaited(); self.sender.send.assert_not_awaited()

    async def test_cached_missing_requires_fresh_query_then_respects_signed(self):
        self.service.record(self.uid, [self.record()]); self.create()
        self.engine.plan_user(self.uid, self.now); self.query.return_value=[self.record("done")]
        await self.dispatcher.dispatch_due()
        self.query.assert_awaited_once(); self.sender.send.assert_not_awaited()

    async def test_unknown_absent_ambiguous_and_failures_never_notify(self):
        self.create(actions=2); self.engine.plan_user(self.uid, self.now)
        self.query.return_value=[]
        await self.dispatcher.dispatch_due()
        self.query.assert_awaited_once(); self.sender.send.assert_not_awaited()
        for rows in ([self.record("unknown")], [self.record(),self.record(external_id="duplicate")], None):
            self.create(actions=2); self.engine.plan_user(self.uid, self.now)
            self.service.day_reads.clear()
            self.query.reset_mock(); self.query.return_value=rows
            if rows is None: self.query.side_effect=RuntimeError("private upstream response")
            await self.dispatcher.dispatch_due()
            self.query.assert_awaited_once(); self.sender.send.assert_not_awaited()
        self.assertNotIn("private upstream", str(self.db.list_triggers(self.uid)))

    async def test_same_time_rules_share_day_query(self):
        self.create(); self.create(); self.engine.plan_user(self.uid,self.now)
        self.query.return_value=[self.record()]
        await self.dispatcher.dispatch_due()
        self.query.assert_awaited_once(); self.assertEqual(2,self.sender.send.await_count)

    async def test_result_survives_service_restart_and_notification_retry(self):
        self.create(); self.engine.plan_user(self.uid,self.now)
        self.query.return_value=[self.record()]; self.sender.send.side_effect=RuntimeError("send failed")
        await self.dispatcher.dispatch_due()
        trigger=self.db.list_triggers(self.uid)[0]
        self.db.reschedule_trigger(trigger["id"],self.now.isoformat(),"retry")
        self.dispatcher.attendance_checker=AttendanceService(self.db,self.query).prepare
        await self.dispatcher.dispatch_due()
        self.query.assert_awaited_once(); self.assertEqual(2,self.sender.send.await_count)

    async def test_later_retry_rechecks_missing_and_cancels_after_signing(self):
        self.create(); self.engine.plan_user(self.uid, self.now)
        self.query.return_value = [self.record()]
        self.sender.send.side_effect = RuntimeError("send failed")
        await self.dispatcher.dispatch_due()
        trigger = self.db.list_triggers(self.uid)[0]
        self.db.reschedule_trigger(trigger["id"], datetime.now(UTC).isoformat(), "later retry")
        self.engine.plan_user(self.uid, self.now + timedelta(minutes=2))
        self.query.return_value = [self.record("done")]
        await self.dispatcher.dispatch_due()
        self.assertEqual(2, self.query.await_count)
        self.assertEqual(1, self.sender.send.await_count)
        self.assertEqual("cancelled", self.db.list_triggers(self.uid)[0]["status"])

    async def test_quiet_delay_queries_at_delivery_instead_of_at_original_point(self):
        self.create(); self.engine.plan_user(self.uid, self.now)
        with patch.object(self.dispatcher, "_quiet_decision", return_value=self.now + timedelta(minutes=2)):
            await self.dispatcher.dispatch_due()
        self.query.assert_not_awaited()
        trigger = self.db.list_triggers(self.uid)[0]
        self.db.reschedule_trigger(trigger["id"], datetime.now(UTC).isoformat(), "quiet ended")
        self.query.return_value = [self.record("done")]
        await self.dispatcher.dispatch_due()
        self.query.assert_awaited_once()
        self.sender.send.assert_not_awaited()

    async def test_disable_or_ignore_during_query_blocks_delivery(self):
        self.create(); self.engine.plan_user(self.uid,self.now)
        async def disable(*args):
            self.db.update_source_settings(self.uid,"iclass",enabled=False)
            return [self.record()]
        self.query.side_effect=disable
        await self.dispatcher.dispatch_due(); self.sender.send.assert_not_awaited()

    async def test_checkpoint_health_preserves_settings_and_manual_sync_history(self):
        import app.main as main
        from app.upstream.sso import AuthenticationRequired
        self.db.upsert_source(self.uid,{"id":"iclass","label":"签到","status":"healthy","event_count":2,"last_sync_at":self.now.isoformat()})
        adapter=AsyncMock(); adapter.query_day.side_effect=AuthenticationRequired("expired")
        from types import SimpleNamespace
        import httpx
        class School(SimpleNamespace):
            @property
            def authenticated(self): return self.user is not None
        school = School(user={},network_mode="direct",client=SimpleNamespace(cookies=httpx.Cookies()))
        with patch.object(main,"db",self.db), patch.object(main,"source_session",AsyncMock(return_value=school)), patch.object(main,"IClassAdapter",return_value=adapter):
            with self.assertRaises(AuthenticationRequired): await main.query_attendance_day(self.uid,self.now.date())
            self.assertEqual("login_required",self.db.get_source(self.uid,"iclass")["status"])
            adapter.query_day.side_effect=None; adapter.query_day.return_value=[]
            school.user={}  # Explicit school login restored the session.
            self.assertEqual([],await main.query_attendance_day(self.uid,self.now.date()))
            source=self.db.get_source(self.uid,"iclass")
            self.assertEqual("healthy",source["status"]); self.assertEqual(2,source["event_count"])
            self.assertEqual(self.now.isoformat(),source["last_sync_at"])
            self.assertTrue(source["enabled"])

    async def test_out_of_window_and_stale_point_do_not_query(self):
        self.create(); self.engine.plan_user(self.uid,self.now)
        trigger=self.db.list_triggers(self.uid)[0]
        self.db.reschedule_trigger(trigger["id"],(self.now-timedelta(minutes=3)).isoformat(),"expired")
        await self.dispatcher.dispatch_due()
        self.query.assert_not_awaited(); self.sender.send.assert_not_awaited()

    def test_calendar_and_connection_details_no_duplicates(self):
        self.service.record(self.uid,[self.record("done")])
        self.db.upsert_event(self.uid,self.record("done"))
        plain={**self.raw,"external_id":"plain","title":"普通课程","metadata":{}}
        self.db.upsert_event(self.uid,plain)
        events=merge_calendar(self.db.list_events(self.uid))
        self.assertEqual(2,len(events))
        self.assertEqual(1,sum("attendance" in e for e in events))
        self.assertEqual([], merge_calendar([self.record()]))
        self.assertTrue(all("attendance" not in e for e in merge_calendar(events,False)))
        self.assertNotIn("attendance", merge_calendar([{"kind":"assignment", "metadata":{}}])[0])
        self.assertTrue(self.service.details(self.uid)[0]["matched"])

    def test_signed_is_monotonic_but_changed_times_invalidate_cache(self):
        self.service.record(self.uid,[self.record("done")]); self.service.record(self.uid,[self.record()])
        self.db.upsert_event(self.uid,self.raw)
        self.assertEqual("done",cached_status(self.db.event_by_id(self.uid,self.course["id"])))
        self.db.upsert_event(self.uid,{**self.raw,"starts_at":(self.now+timedelta(days=1)).isoformat()})
        self.assertEqual("unknown",cached_status(self.db.event_by_id(self.uid,self.course["id"])))

    def test_ambiguous_mapping_does_not_guess(self):
        self.db.upsert_event(self.uid,{**self.raw,"external_id":"duplicate"})
        self.service.record(self.uid,[self.record("done")])
        self.assertEqual("unknown",cached_status(self.db.event_by_id(self.uid,self.course["id"])))

    def test_actual_window_dedup_exclusion_and_source_opt_in(self):
        self.create([{"anchor":"starts_at","offset_minutes":-10},
                     {"anchor":"starts_at","offset_minutes":60},
                     {"anchor":"ends_at","offset_minutes":0},
                     {"anchor":"starts_at","offset_minutes":120}])
        self.engine.plan_user(self.uid,self.now-timedelta(minutes=10))
        self.assertEqual(2,len(self.db.list_triggers(self.uid)))
        self.db.update_source_settings(self.uid,"iclass",enabled=False)
        self.engine.plan_user(self.uid,self.now-timedelta(minutes=10))
        self.assertTrue(all(t["status"]=="cancelled" for t in self.db.list_triggers(self.uid)))
        self.db.update_source_settings(self.uid,"iclass",enabled=True)
        self.engine.plan_user(self.uid,self.now)
        self.assertEqual(1,sum(t["status"]=="queued" for t in self.db.list_triggers(self.uid)))
        excluded=self.create(excluded=["测试 课程"])
        self.engine.plan_user(self.uid,self.now)
        self.assertFalse(any(t["rule_id"]==excluded["id"] for t in self.db.list_triggers(self.uid)))
        self.query.assert_not_awaited()

    def test_new_source_defaults_disabled_and_init_migration_only_once(self):
        self.db.upsert_source(self.other,{"id":"iclass","label":"签到","status":"not_connected"})
        self.assertFalse(self.db.get_source(self.other,"iclass")["enabled"])
        self.assertEqual(15,self.db.get_source(self.other,"iclass")["refresh_interval_minutes"])
        self.db.init(); self.assertTrue(self.db.get_source(self.uid,"iclass")["enabled"])

    async def test_automatic_sync_respects_saved_interval_and_opt_out(self):
        import app.main as main
        from unittest.mock import Mock
        self.db.upsert_school_connection(self.uid,{"school_id":"fixture","remember_password":True,"password_ciphertext":"fixture"})
        self.db.upsert_source(self.uid,{"id":"iclass","label":"签到","status":"healthy","last_sync_at":self.now.isoformat()})
        self.db.update_source_settings(self.uid,"iclass",refresh_interval_minutes=15,network_mode="webvpn")
        self.db.upsert_source(self.uid,{"id":"iclass","label":"签到","status":"healthy","last_sync_at":self.now.isoformat()})
        self.assertNotIn("iclass",self.db.due_source_ids(self.uid,self.now+timedelta(minutes=14)))
        self.assertIn("iclass",self.db.due_source_ids(self.uid,self.now+timedelta(minutes=15)))
        self.db.upsert_source(self.uid,{"id":"iclass","label":"签到","status":"healthy","last_sync_at":(self.now-timedelta(minutes=16)).isoformat()})
        with patch.object(main,"db",self.db), patch.object(main,"refresh_school_auth",AsyncMock()), patch.object(main,"auth_jobs",{}), patch.object(main,"start_sync",Mock()) as start:
            await main.sync_all_remembered(); start.assert_called_once_with(self.uid,{"iclass"})
            start.reset_mock(); self.db.update_source_settings(self.uid,"iclass",refresh_interval_minutes=0)
            await main.sync_all_remembered(); start.assert_not_called()
            self.db.update_source_settings(self.uid,"iclass",refresh_interval_minutes=5,enabled=False)
            await main.sync_all_remembered(); start.assert_not_called()
        self.db.init(); self.assertEqual(5,self.db.get_source(self.uid,"iclass")["refresh_interval_minutes"])

    def test_validator_guards_bounds_duplicates_and_delayed_actions(self):
        for points in ([{"anchor":"starts_at","offset_minutes":-11}],
                       [{"anchor":"ends_at","offset_minutes":1}],
                       [{"anchor":"starts_at","offset_minutes":True}],
                       [{"anchor":"starts_at","offset_minutes":0}]*2):
            with self.assertRaises(ValueError): AutomationRuleCreate.model_validate(rule_payload(points))
        payload=rule_payload(); payload["actions"][0]["after_minutes"]=1
        with self.assertRaises(ValueError): AutomationRuleCreate.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
