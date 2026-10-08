import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

import app.main as main
from app.adapters.iclass import IClassAdapter, MY_CENTER_URL
from app.adapters.judge import JudgeAdapter
from app.adapters.spoc import SpocAdapter, SUBMISSION_URL
from app.db import Database
from app.models import PreferencesUpdate
from app.services.academic import CHINA, academic_window, in_academic_window
from app.services.aggregator import Aggregator
from app.services.event_state import public_event, reminder_matches
from app.services.rules import RuleEngine


class AcademicTest(unittest.TestCase):
    def test_overlapping_default_windows(self):
        for day, start, end in [
            ("2026-02-10", "2025-08-01", "2026-09-01"),
            ("2026-03-10", "2026-02-01", "2026-09-01"),
            ("2026-08-10", "2026-02-01", "2027-03-01"),
            ("2026-09-10", "2026-08-01", "2027-03-01"),
            ("2026-01-10", "2025-08-01", "2026-03-01"),
        ]:
            window = academic_window({}, datetime.fromisoformat(day).replace(tzinfo=CHINA))
            self.assertEqual((start, end), (window["start"], window["end"]))

    def test_custom_window_boundaries_and_unknown_time(self):
        window = academic_window({"academic_start_date": "2024-08-01", "academic_end_date": "2025-03-01"})
        self.assertTrue(in_academic_window({"kind": "assignment", "due_at": "2024-08-01T00:00:00+08:00"}, window))
        self.assertFalse(in_academic_window({"kind": "assignment", "due_at": "2025-03-01T00:00:00+08:00"}, window))
        self.assertTrue(in_academic_window({"kind": "assignment"}, window))
        self.assertTrue(in_academic_window({"kind": "assignment", "due_at": "2020-01-01"}, {**window, "enabled": False}))

    def test_preferences_persist_without_overwriting_quiet_hours(self):
        db = Database(":memory:"); db.init()
        user = db.create_user("prefs@example.com", "Prefs", "hash")
        db.update_preferences(user["id"], {"quiet_start": "23:00", "quiet_end": "07:00"})
        values = PreferencesUpdate(academic_start_date="2024-08-01", academic_end_date="2025-03-01")
        prefs = db.update_preferences(user["id"], values.model_dump(mode="json", exclude_unset=True))
        self.assertEqual("23:00", prefs["quiet_start"])
        db.update_preferences(user["id"], {"quiet_end": "08:00"})
        self.assertEqual("2024-08-01", db.preferences(user["id"])["academic_start_date"])
        with self.assertRaises(ValueError):
            PreferencesUpdate(academic_start_date="2026-09-01", academic_end_date="2026-08-01")

    def test_dashboard_filter_is_tenant_scoped_and_non_destructive(self):
        db = Database(":memory:"); db.init()
        user = db.create_user("term@example.com", "Term", "hash")
        db.update_preferences(user["id"], {"academic_start_date": "2026-08-01", "academic_end_date": "2027-03-01"})
        for key, deadline in [("old", "2024-09-01"), ("current", "2026-10-10")]:
            db.upsert_event(user["id"], {"source": "judge", "external_id": key, "kind": "assignment", "title": key, "due_at": deadline})
        main.app.dependency_overrides[main.current_user] = lambda: user
        client = TestClient(main.app)
        try:
            with patch.object(main, "db", db), patch.object(main, "rule_engine", RuleEngine(db, True)):
                self.assertEqual(["current"], [e["title"] for e in client.get("/api/dashboard").json()["events"]])
                db.update_preferences(user["id"], {"academic_filter_enabled": False})
                self.assertEqual(2, len(client.get("/api/events").json()))
                self.assertEqual(2, len(db.list_events(user["id"])))
        finally:
            client.close()
            main.app.dependency_overrides.clear()


class StatusTest(unittest.TestCase):
    def test_unknown_and_partial_are_not_completed(self):
        adapter = SpocAdapter(object())
        event = adapter._event("2026", {"tjzt": "其他", "zyid": "1"})
        self.assertEqual("unknown", event["status"])
        self.assertEqual("状态未知", public_event(event)["status_label"])
        self.assertEqual("unsubmitted", adapter.submission_status(None, False))
        html = '''作业时间：2030-08-01 08:00 至 2030-09-01 23:59 共 2 道 作业类型：编程
            <table><tr><td>1</td><td>题目甲</td><td>10</td><td>得分：5 最后一次提交时间</td></tr>
            <tr><td>2</td><td>题目乙</td><td>10</td><td>还未提交代码</td></tr></table>'''
        event = JudgeAdapter(object())._event("1", "课程", "2", "作业", html)
        self.assertEqual("partial", event["status"])
        self.assertEqual(1, event["metadata"]["submitted_count"])
        self.assertEqual(2, event["metadata"]["total_problems"])
        self.assertEqual("编程", event["metadata"]["assignment_type"])
        self.assertFalse(event["metadata"]["submitted"])

    def test_reminder_status_type_and_expiry(self):
        now = datetime(2026, 10, 7, tzinfo=UTC)
        event = {"kind": "assignment", "status": "done", "due_at": "2026-10-10T00:00:00Z",
                 "metadata": {"submission_status": "submitted", "assignment_type": "编程"}}
        self.assertFalse(reminder_matches({}, event, now))
        rule = {"profile_settings": {"completion_filter": "submitted", "assignment_type": "编程"}}
        self.assertTrue(reminder_matches(rule, event, now))
        self.assertFalse(reminder_matches(rule, {**event, "due_at": "2025-10-10"}, now))
        self.assertFalse(reminder_matches(rule, {**event, "metadata": {"submission_status": "partial"}}, now))


class SyncTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:"); self.db.init()
        self.user = self.db.create_user("sync@example.com", "Sync", "hash")

    async def test_manual_jobs_return_immediately_and_are_deduplicated(self):
        gate = asyncio.Event()
        async def slow(uid, source):
            await gate.wait()
            return 3
        with patch.object(main, "db", self.db), patch.object(main, "sync_jobs", {}), patch.object(main, "sync_progress", {}), patch.object(main, "sync_one", side_effect=slow) as run:
            one = await main.sync_source("judge", self.user)
            self.assertEqual(["judge"], one["started"])
            task = main.sync_jobs[(self.user["id"], "judge")]
            await main.sync_source("judge", self.user)
            self.assertIs(task, main.sync_jobs[(self.user["id"], "judge")])
            main.start_sync(self.user["id"], {"spoc", "byxt"})
            self.assertEqual(3, len(main.sync_jobs))
            await asyncio.sleep(0)
            self.assertEqual(3, run.call_count)
            gate.set()
            await asyncio.gather(*main.sync_jobs.values())

    async def test_failed_partial_sync_keeps_old_events(self):
        uid = self.user["id"]
        self.db.upsert_event(uid, {"source": "judge", "external_id": "old", "kind": "assignment", "title": "旧任务"})
        adapter = SimpleNamespace(id="judge", label="JUDGE", last_error_count=1, last_error_detail="部分详情失败",
            sync=AsyncMock(return_value=[{"source": "judge", "external_id": "new", "kind": "assignment", "title": "新任务"}]))
        await Aggregator(self.db, uid, [adapter]).sync()
        self.assertEqual(2, len(self.db.list_events(uid)))
        self.assertEqual("degraded", self.db.get_source(uid, "judge")["status"])

    async def test_iclass_retries_missing_login_name_once(self):
        session = SimpleNamespace(authenticated=True)
        adapter = IClassAdapter(session)
        from app.upstream.sso import AuthenticationRequired
        with patch.object(adapter, "_sync_session", side_effect=[AuthenticationRequired("missing"), []]) as query:
            self.assertEqual([], await adapter.sync())
            self.assertEqual(2, query.await_count)

    async def test_iclass_redirect_keeps_encoded_plus(self):
        urls = []
        def upstream(request):
            urls.append(str(request.url))
            if len(urls) == 1:
                return httpx.Response(302, headers={"location": "/?type=jumpMyCenter&loginName=a%2Bb"})
            raise AssertionError("should parse Location before following")
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            adapter = IClassAdapter(SimpleNamespace(client=client))
            self.assertEqual("a+b", await adapter._resolve_login_name())
            self.assertEqual([MY_CENTER_URL], urls)

    async def test_judge_out_of_term_cache_avoids_detail_requests(self):
        old = {"source": "judge", "external_id": "assignment-12-34", "kind": "assignment", "title": "旧作业", "due_at": "2024-10-01"}
        adapter = JudgeAdapter(SimpleNamespace(authenticated=True, client=SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200, request=httpx.Request("GET", "https://judge.buaa.edu.cn"), text="ok")))), [old], {"enabled": True, "start": "2026-08-01", "end": "2027-03-01"})
        async def pages(url):
            if "courseID=0" in url: return '<a href="courselist.jsp?courseID=12">课程</a>'
            if "assignID" in url: raise AssertionError("old assignment detail fetched")
            return '<a href="index.jsp?assignID=34">作业</a>'
        with patch.object(adapter, "_get", side_effect=pages):
            self.assertEqual([old], await adapter.sync())


class IgnoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_ignore_preserves_school_status_survives_sync_and_restores_future_reminders(self):
        db = Database(":memory:"); db.init()
        user = db.create_user("ignore@example.com", "Ignore", "hash")
        db.update_preferences(user["id"], {"academic_filter_enabled": False})
        event = {"source": "spoc", "external_id": "1", "kind": "assignment", "title": "任务",
                 "status": "pending", "due_at": (datetime.now(UTC) + timedelta(days=2)).isoformat()}
        db.upsert_event(user["id"], event)
        saved = db.event_by_key(user["id"], "spoc", "1")
        db.create_automation_rule(user["id"], {"name": "提醒", "enabled": True,
            "scope": {"kinds": ["assignment"], "sources": ["spoc"]},
            "trigger": {"anchor": "due_at", "offset_minutes": -60},
            "actions": [{"channel": "in_app", "after_minutes": 0}], "quiet_hours_policy": "ignore"})
        engine = RuleEngine(db, True); engine.plan_user(user["id"])
        self.assertEqual("queued", db.list_triggers(user["id"])[0]["status"])
        ignored = db.set_event_ignored(user["id"], saved["id"], True)
        engine.plan_user(user["id"])
        self.assertEqual("pending", ignored["status"])
        self.assertEqual("已忽略", public_event(ignored)["status_label"])
        self.assertEqual("cancelled", db.list_triggers(user["id"])[0]["status"])
        db.upsert_event(user["id"], event)
        self.assertTrue(db.event_by_id(user["id"], saved["id"])["ignored"])
        db.set_event_ignored(user["id"], saved["id"], False)
        engine.plan_user(user["id"])
        self.assertEqual("queued", db.list_triggers(user["id"])[0]["status"])

    async def test_ignored_reminder_is_never_sent(self):
        from app.services.notifications import TriggerDispatcher
        db = Database(":memory:"); db.init()
        user = db.create_user("ignore-send@example.com", "Ignore", "hash")
        db.upsert_event(user["id"], {"source": "spoc", "external_id": "1", "kind": "assignment", "title": "任务",
            "starts_at": datetime.now(UTC).isoformat(), "status": "pending"})
        db.create_automation_rule(user["id"], {"name": "提醒", "enabled": True,
            "scope": {"kinds": ["assignment"], "sources": ["spoc"]},
            "trigger": {"anchor": "starts_at", "offset_minutes": 0}, "actions": [{"channel": "in_app"}]})
        RuleEngine(db).plan_user(user["id"])
        event = db.event_by_key(user["id"], "spoc", "1")
        db.set_event_ignored(user["id"], event["id"], True)
        sender = AsyncMock()
        await TriggerDispatcher(db, sender).dispatch_due()
        sender.send.assert_not_awaited()

    async def test_other_user_cannot_ignore_an_event(self):
        db = Database(":memory:"); db.init()
        owner = db.create_user("owner@example.com", "Owner", "hash")
        other = db.create_user("other@example.com", "Other", "hash")
        db.upsert_event(owner["id"], {"source": "judge", "external_id": "1", "kind": "assignment", "title": "任务"})
        saved = db.event_by_key(owner["id"], "judge", "1")
        self.assertIsNone(db.set_event_ignored(other["id"], saved["id"], True))
        main.app.dependency_overrides[main.csrf_user] = lambda: other
        client = TestClient(main.app)
        try:
            with patch.object(main, "db", db):
                self.assertEqual(404, client.patch(f"/api/events/{saved['id']}/ignore", json={"ignored": True}).status_code)
        finally:
            main.app.dependency_overrides.clear(); client.close()


if __name__ == "__main__":
    unittest.main()
