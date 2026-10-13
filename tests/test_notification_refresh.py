import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from app.db import Database, now_iso
from app.services.notifications import TriggerDispatcher
from app.services.rules import RuleEngine
from app.services.sync_changes import SyncChangeTracker


class NotificationRefreshTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.init()
        self.uid = self.db.create_user("refresh@example.test", "Test", "hash")["id"]
        self.engine = RuleEngine(self.db)
        self.sender = AsyncMock()
        self.refresh = AsyncMock(return_value=(True, ""))
        self.dispatcher = TriggerDispatcher(self.db, self.sender, source_refresher=self.refresh)

    def tearDown(self):
        self.db.close()

    def add(self, source="spoc", uid=None, actions=1, kind="assignment", key="task"):
        uid = uid or self.uid
        now = datetime.now(UTC)
        event = {"source": source, "external_id": key, "kind": kind, "title": key,
                 "due_at": (now + timedelta(hours=1, seconds=-1)).isoformat() if kind == "assignment" else None,
                 "starts_at": now.isoformat() if kind == "course" else None,
                 "ends_at": (now + timedelta(hours=2)).isoformat() if kind == "course" else None,
                 "status": "pending", "metadata": {"submission_status": "unsubmitted"}}
        self.db.upsert_event(uid, event)
        self.db.upsert_source(uid, {"id": source, "label": source, "status": "healthy"})
        rule = self.db.create_automation_rule(uid, {
            "name": "测试规则", "enabled": True,
            "profile": "course" if kind == "course" else f"{source}-assignment",
            "scope": {"sources": [source], "kinds": [kind]},
            "trigger": {"anchor": "starts_at" if kind == "course" else "due_at", "offset_minutes": 0 if kind == "course" else -60},
            "conditions": [], "cancel_conditions": [], "actions": [{"channel": "in_app"} for _ in range(actions)],
            "quiet_hours_policy": "ignore"})
        self.engine.plan_user(uid)
        return event, rule

    async def test_completed_assignment_is_cancelled_after_refresh(self):
        event, _ = self.add()
        async def refresh(uid, source):
            self.db.upsert_event(uid, {**event, "status": "done", "metadata": {"submission_status": "submitted"}})
            self.engine.plan_user(uid)
            return True, ""
        self.refresh.side_effect = refresh
        await self.dispatcher.dispatch_due()
        self.sender.send.assert_not_awaited()
        self.refresh.assert_awaited_once_with(self.uid, "spoc")

    async def test_partial_refresh_never_sends_cached_notice_and_retry_survives_replanning(self):
        self.add()
        self.refresh.return_value = (False, "来源同步不完整")
        await self.dispatcher.dispatch_due()
        before = self.db.list_triggers(self.uid)[0]
        self.assertEqual(1, before["refresh_attempts"])
        self.assertEqual(0, before["attempts"])
        self.assertIn("通知前同步未成功", before["detail"])
        self.engine.plan_user(self.uid, datetime.now(UTC) + timedelta(minutes=20))
        after = self.db.list_triggers(self.uid)[0]
        self.assertEqual(before["run_at"], after["run_at"])
        self.assertEqual(before["dedupe_key"], after["dedupe_key"])
        self.sender.send.assert_not_awaited()

    async def test_channels_share_refresh_and_success_resets_only_refresh_retries(self):
        self.add(actions=2)
        self.refresh.return_value = (False, "暂不可用")
        await self.dispatcher.dispatch_due()
        for trigger in self.db.list_triggers(self.uid):
            self.db.reschedule_trigger(trigger["id"], (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), increment_attempts=True)
        self.refresh.reset_mock()
        self.refresh.return_value = (True, "")
        await self.dispatcher.dispatch_due()
        self.refresh.assert_awaited_once()
        self.assertEqual(2, self.sender.send.await_count)
        for trigger in self.db.list_triggers(self.uid):
            self.assertEqual(0, trigger["refresh_attempts"])
            self.assertEqual(1, trigger["attempts"])

    async def test_deadline_moved_does_not_send_old_scheduled_notice(self):
        event, _ = self.add()
        async def refresh(uid, source):
            self.db.upsert_event(uid, {**event, "due_at": (datetime.now(UTC) + timedelta(hours=4)).isoformat()})
            self.engine.plan_user(uid)
            return True, ""
        self.refresh.side_effect = refresh
        await self.dispatcher.dispatch_due()
        self.sender.send.assert_not_awaited()
        self.assertEqual(1, len(self.db.list_triggers(self.uid)))
        self.assertGreater(self.db.list_triggers(self.uid)[0]["run_at"], now_iso())

    async def test_course_message_uses_fresh_teacher_and_room(self):
        event, _ = self.add(source="byxt", kind="course")
        async def refresh(uid, source):
            self.db.upsert_event(uid, {**event, "location": "新教室", "metadata": {"teacher": "新教师"}})
            self.engine.plan_user(uid)
            return True, ""
        self.refresh.side_effect = refresh
        await self.dispatcher.dispatch_due()
        body = self.sender.send.await_args.args[4]
        self.assertIn("新教室", body)
        self.assertIn("新教师", body)

    async def test_quiet_deferral_refreshes_only_at_real_delivery_time(self):
        self.add()
        delayed = datetime.now(UTC) + timedelta(minutes=10)
        with patch.object(self.dispatcher, "_quiet_decision", return_value=delayed):
            await self.dispatcher.dispatch_due()
        self.refresh.assert_not_awaited()
        trigger = self.db.list_triggers(self.uid)[0]
        self.db.reschedule_trigger(trigger["id"], (datetime.now(UTC) - timedelta(seconds=1)).isoformat())
        await self.dispatcher.dispatch_due()
        self.refresh.assert_awaited_once()
        self.sender.send.assert_awaited_once()

    async def test_slow_connection_does_not_block_other_user_or_duplicate_active_group(self):
        other = self.db.create_user("other-refresh@example.test", "Other", "hash")["id"]
        self.add(source="judge")
        self.add(uid=other)
        gate, entered = asyncio.Event(), asyncio.Event()
        async def refresh(uid, source):
            if source == "judge":
                entered.set()
                await gate.wait()
            return True, ""
        self.refresh.side_effect = refresh
        first = asyncio.create_task(self.dispatcher.dispatch_due())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.sleep(0)
            self.assertEqual(other, self.sender.send.await_args.args[0])
            await self.dispatcher.dispatch_due()
            self.assertEqual(2, self.refresh.await_count)
        finally:
            gate.set()
            await first
        self.assertEqual(2, self.sender.send.await_count)

    async def test_sync_retry_does_not_cross_assignment_deadline(self):
        event, _ = self.add()
        self.db.upsert_event(self.uid, {**event, "due_at": (datetime.now(UTC) + timedelta(seconds=20)).isoformat()})
        self.refresh.return_value = (False, "不可用")
        await self.dispatcher.dispatch_due()
        self.assertEqual("skipped", self.db.list_triggers(self.uid)[0]["status"])
        self.sender.send.assert_not_awaited()

    async def test_refresh_failures_have_a_separate_bounded_retry_budget(self):
        self.add()
        self.refresh.return_value = (False, "不可用")
        for _ in range(4):
            await self.dispatcher.dispatch_due()
            trigger = self.db.list_triggers(self.uid)[0]
            self.db.reschedule_trigger(trigger["id"], (datetime.now(UTC) - timedelta(seconds=1)).isoformat())
        trigger = self.db.list_triggers(self.uid)[0]
        self.assertEqual("failed", trigger["status"])
        self.assertEqual(3, trigger["refresh_attempts"])
        self.assertEqual(0, trigger["attempts"])
        self.sender.send.assert_not_awaited()

    async def test_cancelled_course_change_survives_refresh_without_live_event(self):
        event, _ = self.add(source="byxt", kind="course")
        self.db.create_automation_rule(self.uid, {"name": "课程变更", "enabled": True, "profile": "course-change",
            "scope": {"sources": ["byxt"], "kinds": ["course"]}, "profile_settings": {"change_types": ["cancelled"]},
            "actions": [{"channel": "in_app"}], "quiet_hours_policy": "ignore"})
        tracker = SyncChangeTracker(self.db, self.uid)
        tracker.record("byxt", self.db.list_events(self.uid), True)
        tracker.record("byxt", [], True)
        self.db.remove_missing_source_events(self.uid, "byxt", set())
        self.engine.plan_user(self.uid)
        await self.dispatcher.dispatch_due()
        self.sender.send.assert_awaited_once()
        self.assertIn("课程取消", self.sender.send.await_args.args[4])

    async def test_main_refresh_joins_running_sync_and_rejects_degraded_result(self):
        import app.main as main
        self.add()
        self.db.update_source_settings(self.uid, "spoc", refresh_interval_minutes=0)
        gate = asyncio.Event()
        async def sync(uid, source):
            await gate.wait()
            self.db.upsert_source(uid, {"id": source, "label": source, "status": "degraded"})
            return 1
        with patch.object(main, "db", self.db), patch.object(main, "sync_jobs", {}), patch.object(main, "sync_progress", {}), patch.object(main, "sync_one", AsyncMock(side_effect=sync)) as run:
            task = main.start_sync(self.uid, {"spoc"})["spoc"]
            refresh = asyncio.create_task(main.refresh_notification_source(self.uid, "spoc"))
            await asyncio.sleep(0)
            gate.set()
            self.assertFalse((await refresh)[0])
            await task
            run.assert_awaited_once()

    async def test_superseded_grade_is_not_delivered(self):
        event, _ = self.add()
        self.db.create_automation_rule(self.uid, {"name": "评分", "enabled": True, "profile": "spoc-assignment-grade",
            "scope": {"sources": ["spoc"], "kinds": ["assignment"]}, "actions": [{"channel": "in_app"}], "quiet_hours_policy": "ignore"})
        tracker = SyncChangeTracker(self.db, self.uid)
        self.db.upsert_event(self.uid, {**event, "metadata": {"grading_known": True, "graded": False}})
        tracker.record("spoc", self.db.list_events(self.uid), True)
        self.db.upsert_event(self.uid, {**event, "status": "done", "metadata": {"grading_known": True, "graded": True, "earned_score": 5}})
        tracker.record("spoc", self.db.list_events(self.uid), True)
        self.engine.plan_user(self.uid)
        async def refresh(uid, source):
            self.db.upsert_event(uid, {**event, "status": "done", "metadata": {"grading_known": True, "graded": True, "earned_score": 9}})
            return True, ""
        self.refresh.side_effect = refresh
        await self.dispatcher.dispatch_due()
        self.sender.send.assert_not_awaited()
        self.assertTrue(any(t["status"] == "cancelled" for t in self.db.list_triggers(self.uid)))

    async def test_worker_keeps_periodic_scheduler_running_during_slow_dispatch_and_cleans_up(self):
        from app.services.worker import BackgroundWorker
        entered, scheduled, gate = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def dispatch():
            entered.set()
            await gate.wait()
        async def schedule():
            scheduled.set()
        dispatcher = AsyncMock()
        dispatcher.dispatch_due.side_effect = dispatch
        worker = BackgroundWorker(dispatcher, schedule, interval=0.001, sync_interval=0.001)
        worker.start()
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(scheduled.wait(), 1)
            self.assertTrue(worker.dispatch_tasks)
        finally:
            await worker.stop()
        self.assertFalse(worker.dispatch_tasks)

    async def test_dispatch_cancellation_does_not_cancel_shared_manual_sync(self):
        import app.main as main
        self.add()
        gate = asyncio.Event()
        async def sync(uid, source):
            await gate.wait()
            return 1
        with patch.object(main, "db", self.db), patch.object(main, "sync_jobs", {}), patch.object(main, "sync_progress", {}), patch.object(main, "sync_one", AsyncMock(side_effect=sync)):
            task = main.start_sync(self.uid, {"spoc"})["spoc"]
            refresh = asyncio.create_task(main.refresh_notification_source(self.uid, "spoc"))
            await asyncio.sleep(0)
            refresh.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await refresh
            self.assertFalse(task.cancelled())
            gate.set()
            await task
