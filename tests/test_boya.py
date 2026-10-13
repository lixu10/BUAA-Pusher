import asyncio
import copy
import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.adapters.bykc import BykcAdapter
from app.db import Database
from app.models import AutomationRuleCreate
from app.services.boya import catalog_matches, enrollment_state, filter_matches, record_catalog
from app.services.notifications import TriggerDispatcher
from app.services.rules import RuleEngine


def course(key=1, remaining=0, opens=None, **values):
    now = datetime.now(UTC)
    raw = {"id": key, "courseName": "测试美育课程", "courseTeacher": "测试教师", "coursePosition": "测试教室",
           "courseStartDate": (now + timedelta(hours=4)).isoformat(), "courseEndDate": (now + timedelta(hours=5)).isoformat(),
           "courseSelectStartDate": (opens or now - timedelta(hours=1)).isoformat(),
           "courseSelectEndDate": (now + timedelta(hours=2)).isoformat(),
           "courseMaxCount": 10, "courseCurrentCount": 10 - remaining, "selected": False,
           "courseNewKind2": {"id": 22, "kindName": "美育"}, "courseCampus": "[1]",
           "courseSignType": 2, **values}
    adapter = BykcAdapter(object())
    adapter.config = {"campus": [{"id": 1, "campusName": "学院路校区"}]}
    return adapter.catalog_event(raw)


def definition(profile, **settings):
    return AutomationRuleCreate(name="博雅规则", profile=profile, scope={"sources": ["bykc"], "kinds": ["boya_catalog"]},
        actions=[{"channel": "in_app"}], quiet_hours_policy="ignore", profile_settings=settings).model_dump()


class BoyaProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_full_catalog_pagination_and_read_only_parameters(self):
        adapter = BykcAdapter(AsyncMock(authenticated=True))
        adapter._login = AsyncMock()
        now = datetime.now(UTC)
        rows = [{"id": 1, "courseName": "课程1"}, {"id": 2, "courseName": "课程2"}]
        adapter._call = AsyncMock(side_effect=[{}, {"content": [rows[0]], "totalPages": 2, "totalElements": 2},
                                              {"content": [rows[1]], "totalPages": 2, "totalElements": 2}])
        self.assertEqual(2, len(await adapter.catalog()))
        self.assertEqual(["getAllConfig", "queryStudentSemesterCourseByPage", "queryStudentSemesterCourseByPage"],
                         [call.args[0] for call in adapter._call.call_args_list])
        self.assertEqual(2, adapter._call.call_args.args[1]["pageNumber"])

    async def test_duplicate_missing_or_incomplete_catalog_is_not_success(self):
        for response in [{"content": [], "totalPages": 1, "totalElements": 2},
                         {"content": [{"id": 1}, {"id": 1}], "totalPages": 1, "totalElements": 2},
                         {"content": [{"courseName": "缺标识"}], "totalPages": 1, "totalElements": 1},
                         {"totalPages": 0, "totalElements": 0}]:
            adapter = BykcAdapter(AsyncMock(authenticated=True))
            adapter._login = AsyncMock(); adapter._call = AsyncMock(side_effect=[{}, response])
            with self.assertRaises(RuntimeError):
                await adapter.catalog()

    async def test_details_check_identity_and_never_call_mutating_routes(self):
        adapter = BykcAdapter(AsyncMock(authenticated=True)); adapter._login = AsyncMock()
        adapter._call = AsyncMock(side_effect=[{}, {"id": 7, "courseName": "课程"}])
        self.assertEqual("7", (await adapter.course_details(["7"]))[0]["metadata"]["course_id"])
        self.assertEqual("queryCourseById", adapter._call.call_args.args[0])
        self.assertEqual({"id": 7}, adapter._call.call_args.args[1])
        adapter._call = AsyncMock(side_effect=[{}, {"id": 8}])
        with self.assertRaises(RuntimeError): await adapter.course_details(["7"])

    async def test_mutating_routes_are_rejected_before_any_network_call(self):
        adapter=BykcAdapter(AsyncMock(authenticated=True))
        for route in ["choseCourse", "delChosenCourse", "signCourseByUser"]:
            with self.assertRaises(ValueError): await adapter._call(route,{"id":1})
        adapter.session.client.post.assert_not_awaited()

    def test_private_nested_user_and_location_coordinates_not_stored(self):
        event = course(courseBelongUser={"studentNo": "PRIVATE-FIXTURE", "ipAddress": "PRIVATE-FIXTURE"},
                       courseSignConfig=json.dumps({"signStartDate": "2030-09-19 10:00:00", "signPointList": [{"lat": 99, "lng": 99}]}))
        encoded = json.dumps(event)
        self.assertNotIn("PRIVATE-FIXTURE", encoded)
        self.assertNotIn("signPointList", encoded)
        self.assertIsNotNone(event["metadata"]["sign_windows"]["signStartDate"])


class BoyaRulesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database(":memory:"); self.db.init()
        self.uid = self.db.create_user("boya@example.test", "Test", "hash")["id"]
        self.db.upsert_source(self.uid, {"id": "bykc", "label": "博雅", "status": "healthy", "enabled": True})
        self.engine = RuleEngine(self.db, enforce_feature_scope=True)
        self.sender = AsyncMock()
        self.dispatcher = TriggerDispatcher(self.db, self.sender, enforce_feature_scope=True)
        self.refresh = AsyncMock(return_value=(True, "")); self.dispatcher.catalog_refresher = self.refresh

    def tearDown(self): self.db.close()

    def save(self, events):
        self.db.replace_boya_catalog(self.uid, events)
        record_catalog(self.db, self.uid, events)
        self.engine.plan_user(self.uid)

    def rule(self, profile, **settings):
        return self.db.create_automation_rule(self.uid, definition(profile, **settings))

    async def test_first_baseline_is_silent_and_new_course_filters_are_intersection(self):
        self.rule("boya-new-course", categories=["22"], campuses=["1"], sign_types=["2"], keywords=["美育"])
        self.save([course(1, remaining=2)])
        self.assertEqual([], self.db.list_sync_changes(self.uid))
        self.save([course(1, remaining=2), course(2, remaining=2), course(3, remaining=2, courseSignType=1)])
        await self.dispatcher.dispatch_due()
        self.sender.send.assert_awaited_once()
        self.assertIn("新博雅课程", self.sender.send.await_args.args[4])
        self.assertNotIn("boya_catalog", self.sender.send.await_args.args[4])

    async def test_seat_threshold_and_cooldown_do_not_repeat_for_flicker(self):
        self.rule("boya-seats", min_remaining=2, cooldown_minutes=15)
        self.save([course(remaining=0)])
        self.save([course(remaining=1)])
        await self.dispatcher.dispatch_due(); self.sender.send.assert_not_awaited()
        self.save([course(remaining=3)])
        # Use a real local in-app sender to persist successful notification history.
        from app.security import CredentialVault
        from app.services.notifications import NotificationSender
        from pathlib import Path
        sender = NotificationSender(self.db, CredentialVault(Path.cwd(), "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="))
        self.dispatcher.sender = sender
        await self.dispatcher.dispatch_due()
        self.assertEqual(1, len(self.db.list_inbox(self.uid)))
        self.save([course(remaining=0)]); self.save([course(remaining=3)])
        await self.dispatcher.dispatch_due()
        self.assertEqual(1, len(self.db.list_inbox(self.uid)))

    async def test_enrollment_open_is_scheduled_and_rechecked_without_catalog_rescan(self):
        self.rule("boya-enrollment-open")
        self.save([course(remaining=2, opens=datetime.now(UTC) - timedelta(seconds=1))])
        await self.dispatcher.dispatch_due()
        self.refresh.assert_awaited_once_with(self.uid, ["1"])
        self.sender.send.assert_awaited_once()
        self.assertIn("报名已开放", self.sender.send.await_args.args[4])
        self.assertIn("余量：2", self.sender.send.await_args.args[4])

    async def test_registration_after_planning_cancels_delivery(self):
        self.rule("boya-enrollment-open")
        event = course(remaining=2, opens=datetime.now(UTC) - timedelta(seconds=1))
        self.save([event])
        async def refresh(uid, ids):
            changed = copy.deepcopy(event); changed["metadata"]["selected"] = True
            self.db.save_boya_course(uid, changed)
            self.engine.plan_user(uid)
            return True, ""
        self.refresh.side_effect = refresh
        await self.dispatcher.dispatch_due()
        self.sender.send.assert_not_awaited()

    async def test_opening_time_move_replans_instead_of_sending_old_notice(self):
        self.rule("boya-enrollment-open")
        event = course(remaining=2, opens=datetime.now(UTC) - timedelta(seconds=1)); self.save([event])
        async def refresh(uid, ids):
            changed = course(remaining=2, opens=datetime.now(UTC) + timedelta(minutes=30))
            self.db.save_boya_course(uid, changed); self.engine.plan_user(uid)
            return True, ""
        self.refresh.side_effect = refresh
        await self.dispatcher.dispatch_due(); self.sender.send.assert_not_awaited()
        self.assertTrue(any(t["status"] == "queued" and t["run_at"] > datetime.now(UTC).isoformat() for t in self.db.list_triggers(self.uid)))

    async def test_unknown_capacity_and_selected_courses_do_not_alert(self):
        self.rule("boya-enrollment-open")
        self.save([course(1, courseCurrentCount=None), course(2, selected=True)])
        await self.dispatcher.dispatch_due(); self.sender.send.assert_not_awaited()
        self.assertEqual("unknown", enrollment_state(course(courseCurrentCount=None)))
        self.assertFalse(filter_matches({"campuses": ["2"]}, course()))
        self.assertTrue(filter_matches({"campuses": ["2"]}, course(courseCampus="ALL", courseCampusList=["全部校区"])))

    async def test_advance_points_survive_restart_and_repeat_planning_without_duplication(self):
        self.rule("boya-enrollment-soon", advance_minutes=[30, 10, 1])
        self.save([course(remaining=4, opens=datetime.now(UTC) + timedelta(hours=1))])
        self.assertEqual(3, len(self.db.list_triggers(self.uid)))
        RuleEngine(self.db, enforce_feature_scope=True).plan_user(self.uid)
        self.assertEqual(3, len(self.db.list_triggers(self.uid)))
        for values in ([0], [1, 1], [True], [-1], [10081]):
            with self.assertRaises(ValueError): definition("boya-enrollment-soon", advance_minutes=values)

    async def test_failed_target_refresh_defers_without_sending(self):
        self.rule("boya-enrollment-open")
        self.save([course(remaining=2, opens=datetime.now(UTC) - timedelta(seconds=1))])
        self.refresh.return_value = (False, "暂不可用")
        await self.dispatcher.dispatch_due()
        self.sender.send.assert_not_awaited()
        self.assertEqual(1, self.db.list_triggers(self.uid)[0]["refresh_attempts"])

    def test_only_selected_courses_are_calendar_events(self):
        event = course(remaining=2)
        self.save([event])
        self.assertEqual([], self.db.list_events(self.uid))
        selected = {**event, "kind": "boya", "external_id": "course-1"}
        self.db.upsert_event(self.uid, selected)
        self.db.refresh_boya_selection(self.uid)
        self.assertEqual(1, len(self.db.list_events(self.uid)))
        self.assertTrue(self.db.boya_course(self.uid, "1")["metadata"]["selected"])

    async def test_full_or_removed_course_never_sends_old_open_notification(self):
        for removed in [False,True]:
            with self.subTest(removed=removed):
                rule=self.rule("boya-enrollment-open")
                event=course(11 if removed else 10,remaining=2,opens=datetime.now(UTC)-timedelta(seconds=1))
                self.save([event])
                async def refresh(uid, ids):
                    self.db.replace_boya_catalog(uid, [] if removed else [course(10,remaining=0,opens=datetime.now(UTC)-timedelta(seconds=1))])
                    self.engine.plan_user(uid)
                    return True,""
                self.refresh.side_effect=refresh
                await self.dispatcher.dispatch_due()
                self.sender.send.assert_not_awaited()
                self.db.update_automation_rule(self.uid,rule["id"],{"enabled":False})

    def test_cancelled_future_plans_can_return_without_replaying_sent_plans(self):
        self.rule("boya-enrollment-closing",advance_minutes=[10])
        event=course(remaining=2)
        self.save([event]); queued=[t for t in self.db.list_triggers(self.uid) if t["status"]=="queued"]
        self.assertEqual(1,len(queued)); original=queued[0]["id"]
        self.save([course(remaining=0)])
        self.assertEqual("cancelled",self.db.trigger_context(original)["status"])
        self.save([event])
        self.assertEqual("queued",self.db.trigger_context(original)["status"])
        self.db.finish_trigger(original,"sent")
        self.save([course(remaining=0)]); self.save([event])
        self.assertEqual("sent",self.db.trigger_context(original)["status"])

    def test_same_named_different_boya_ids_are_addition_and_cancellation(self):
        from app.services.sync_changes import compare_courses
        old,new=course(1),course(2)
        old.update(kind="boya",external_id="course-1"); new.update(kind="boya",external_id="course-2")
        changes=compare_courses({"course-1":old},{"course-2":new})
        self.assertEqual({"added","cancelled"},{c["categories"][0] for c in changes})


class BoyaApiTest(unittest.TestCase):
    def setUp(self):
        import app.main as main
        self.main = main; self.db = Database(":memory:"); self.db.init()
        self.user = self.db.create_user("boya-api@example.test", "Test", "hash")
        self.other = self.db.create_user("boya-other@example.test", "Other", "hash")
        self.patch = patch.object(main, "db", self.db); self.patch.start()
        self.engine_patch = patch.object(main, "rule_engine", RuleEngine(self.db, enforce_feature_scope=True)); self.engine_patch.start()
        main.app.dependency_overrides[main.current_user] = lambda: self.user
        main.app.dependency_overrides[main.csrf_user] = lambda: self.user
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close(); self.main.app.dependency_overrides.clear(); self.patch.stop(); self.engine_patch.stop(); self.db.close()

    def test_cache_api_search_paging_and_account_isolation(self):
        for key in range(1, 4): self.db.save_boya_course(self.user["id"], course(key, remaining=key))
        self.db.save_boya_course(self.other["id"], course(99, remaining=2))
        data = self.client.get("/api/boya/courses?size=2&page=1&category=22&campus=1&sign_type=2").json()
        self.assertEqual(3, data["total"]); self.assertEqual(2, len(data["courses"]))
        self.assertEqual(404, self.client.get("/api/boya/courses/99").status_code)
        self.assertEqual(0, self.client.get("/api/boya/courses?campus=2").json()["total"])
        self.assertEqual([], self.client.get("/api/events").json())
        self.assertEqual(409, self.client.post("/api/boya/catalog/sync").status_code)

    def test_boya_opt_in_and_catalog_interval_configuration(self):
        source = next(s for s in self.client.get("/api/dashboard").json()["sources"] if s["id"] == "bykc")
        self.assertTrue(source["available"]); self.assertFalse(source["enabled"])
        self.assertEqual(200, self.client.patch("/api/sources/bykc", json={"enabled": True, "network_mode": "webvpn"}).status_code)
        self.assertEqual(200, self.client.patch("/api/boya/catalog/settings", json={"refresh_interval_minutes": 1}).status_code)
        self.assertEqual(1, self.db.boya_state(self.user["id"])["refresh_interval_minutes"])
        self.db.init(); self.assertTrue(self.db.get_source(self.user["id"], "bykc")["enabled"])
        self.assertEqual(5, self.db.boya_state(self.other["id"])["refresh_interval_minutes"])
        self.assertEqual(422, self.client.patch("/api/boya/catalog/settings", json={"refresh_interval_minutes": -1}).status_code)

    def test_failed_catalog_read_retains_cache_baseline_and_pending_plans(self):
        self.db.upsert_source(self.user["id"],{"id":"bykc","label":"博雅","enabled":True,"status":"healthy"})
        event=course(remaining=2); self.db.save_boya_course(self.user["id"],event); record_catalog(self.db,self.user["id"],[event])
        adapter=AsyncMock(); adapter.catalog.side_effect=TimeoutError()
        recovery=AsyncMock()
        async def run(uid,school,operation,credentials): return await operation()
        recovery.run.side_effect=run
        with patch.object(self.main,"source_session",AsyncMock()), patch.object(self.main,"BykcAdapter",return_value=adapter), patch.object(self.main,"school_recovery",recovery), patch.object(self.main,"notification_sender",AsyncMock()):
            asyncio.run(self.main.sync_boya_catalog(self.user["id"]))
        self.assertEqual("error",self.db.boya_state(self.user["id"])["status"])
        self.assertIsNotNone(self.db.boya_course(self.user["id"],"1"))
        self.assertEqual(1,len(self.db.get_sync_snapshot(self.user["id"],"bykc_catalog")))
