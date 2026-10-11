import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.services.school_recovery import SchoolAuthRecovery
from app.upstream.sso import AuthenticationRequired, LoginFailed


def session(mode="direct", authenticated=True):
    value = SimpleNamespace(network_mode=mode, user={} if authenticated else None,
        iclass_identity=("fixture", "fixture", "fixture", 0),
        client=SimpleNamespace(cookies=httpx.Cookies()), _form={"execution":"fixture"},
        _captcha_id="fixture", reauthentication_error=None, reauthentication_retry_at=None)
    # A real property is important: auth must track invalidation and successful login.
    class State(SimpleNamespace):
        @property
        def authenticated(self):
            return self.user is not None
    value = State(**vars(value))
    value.client.cookies.set("fixture", "fixture")
    async def login(*args):
        value.user = {}
        value.reauthentication_error = None
        value.reauthentication_retry_at = None
        return {"authenticated":True}
    value.login = AsyncMock(side_effect=login)
    return value


class RecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_refreshes_enabled_routes_without_syncing_manual_sources(self):
        import app.main as main
        from app.db import Database
        db=Database(":memory:"); db.init()
        uid=db.create_user("scheduler-auth@example.com","Fixture","hash")["id"]
        db.upsert_school_connection(uid,{"school_id":"fixture","remember_password":True,"password_ciphertext":"fixture"})
        for source,mode in (("spoc","direct"),("judge","webvpn")):
            db.upsert_source(uid,{"id":source,"label":source,"status":"healthy"})
            db.update_source_settings(uid,source,refresh_interval_minutes=0,network_mode=mode)
        jobs={}; release=asyncio.Event(); calls=[]
        async def refresh(user,mode):
            calls.append((user,mode)); await release.wait()
        try:
            with patch.object(main,"db",db), patch.object(main,"auth_jobs",jobs), \
                 patch.object(main,"refresh_school_auth",side_effect=refresh), patch.object(main,"start_sync",Mock()) as sync:
                await main.sync_all_remembered()
                await asyncio.sleep(0)
                await main.sync_all_remembered()
                self.assertEqual({(uid,"direct"),(uid,"webvpn")},set(calls))
                self.assertEqual(2,len(jobs)); sync.assert_not_called()
        finally:
            release.set(); await asyncio.gather(*jobs.values()); db.close()

    async def test_periodic_refresh_uses_configured_successful_auth_age(self):
        recovery, school = SchoolAuthRecovery(), session()
        school.last_authenticated_at = 100
        with patch("app.services.school_recovery.time.monotonic",return_value=100+24*3600-1):
            self.assertFalse(await recovery.refresh_due(1,school,lambda:("fixture","fixture")))
        school.login.assert_not_awaited()
        with patch("app.services.school_recovery.time.monotonic",return_value=100+24*3600):
            self.assertTrue(await recovery.refresh_due(1,school,lambda:("fixture","fixture")))
            self.assertFalse(await recovery.refresh_due(1,school,lambda:("fixture","fixture")))
        self.assertEqual(1,school.login.await_count)
        with patch("app.services.school_recovery.time.monotonic",return_value=school.last_authenticated_at+3600):
            self.assertTrue(await recovery.refresh_due(1,school,lambda:("fixture","fixture"),hours=1))
        self.assertEqual(2,school.login.await_count)

    async def test_periodic_refresh_does_not_clear_manual_session_without_saved_password(self):
        recovery, school = SchoolAuthRecovery(), session()
        school.last_authenticated_at = 0
        with patch("app.services.school_recovery.time.monotonic",return_value=25*3600):
            self.assertFalse(await recovery.refresh_due(1,school,lambda:None))
        self.assertTrue(school.authenticated); self.assertEqual(1,len(school.client.cookies))
        school.login.assert_not_awaited()

    async def test_periodic_block_does_not_clear_captcha_and_never_reads_business_apis(self):
        recovery, school = SchoolAuthRecovery(), session(authenticated=False)
        school.reauthentication_error="需要验证码"
        with self.assertRaises(AuthenticationRequired):
            await recovery.refresh_due(1,school,lambda:("fixture","fixture"))
        self.assertEqual("fixture",school._captcha_id)
        school.login.assert_not_awaited()

    async def test_expired_read_reauthenticates_and_retries_once_for_both_routes(self):
        for mode in ("direct", "webvpn"):
            school = session(mode)
            operation = AsyncMock(side_effect=[AuthenticationRequired("expired"), ["records"]])
            credentials = Mock(return_value=("fixture", "fixture-password"))
            self.assertEqual(["records"], await SchoolAuthRecovery().run(1,school,operation,credentials))
            school.login.assert_awaited_once_with("fixture","fixture-password")
            self.assertEqual(2,operation.await_count)
            self.assertIsNone(school.iclass_identity)
            self.assertEqual({},school._form)
            self.assertEqual(0,len(school.client.cookies))

    async def test_network_and_business_failures_never_login_or_clear_auth(self):
        for error in (httpx.ConnectTimeout("timeout"), httpx.ConnectError("dns"),
                      httpx.ReadTimeout("timeout"), RuntimeError("STATUS=2")):
            school = session()
            operation = AsyncMock(side_effect=error)
            credentials = Mock()
            with self.assertRaises(type(error)):
                await SchoolAuthRecovery().run(1,school,operation,credentials)
            credentials.assert_not_called(); school.login.assert_not_awaited()
            self.assertTrue(school.authenticated)
            self.assertEqual(1,operation.await_count)

    async def test_network_failure_during_renewal_is_not_password_rejection(self):
        school = session()
        school.login.side_effect = httpx.ConnectError("dns")
        with self.assertRaises(httpx.ConnectError):
            await SchoolAuthRecovery().run(1,school,
                AsyncMock(side_effect=AuthenticationRequired("expired")),lambda:("fixture","fixture"))
        self.assertIsNone(school.reauthentication_error)

    async def test_uc_validation_propagates_transport_errors_during_login(self):
        from app.upstream.sso import BuaaSsoSession
        school = BuaaSsoSession()
        try:
            with patch.object(school.client,"get",AsyncMock(side_effect=httpx.ConnectError("dns"))):
                with self.assertRaises(httpx.ConnectError): await school.refresh_status(strict=True)
                self.assertFalse((await school.refresh_status())["authenticated"])
        finally: await school.close()

    async def test_explicit_login_clears_automatic_recovery_block(self):
        from app.upstream.sso import BuaaSsoSession
        school = BuaaSsoSession()
        school.reauthentication_error = "验证码"
        school._form = {"execution":"fixture"}
        async def validated(*args,**kwargs):
            school.user={}; return {"authenticated":True,"user":{}}
        try:
            with patch.object(school.client,"post",AsyncMock(return_value=httpx.Response(200,text="ok"))), \
                 patch.object(school.client,"get",AsyncMock()), \
                 patch.object(school,"refresh_status",AsyncMock(side_effect=validated)):
                await school.login("fixture","fixture")
            self.assertIsNone(school.reauthentication_error)
            self.assertIsNone(school.reauthentication_retry_at)
        finally: await school.close()

    async def test_missing_saved_password_never_attempts_login(self):
        school = session()
        operation = AsyncMock(side_effect=AuthenticationRequired("expired"))
        with self.assertRaises(AuthenticationRequired):
            await SchoolAuthRecovery().run(1,school,operation,lambda:None)
        school.login.assert_not_awaited(); self.assertFalse(school.authenticated)

    async def test_captcha_and_bad_password_stop_automatic_login_until_manual_success(self):
        for outcome in ({"authenticated":False,"captcha_required":True}, LoginFailed("private-fixture")):
            recovery, school = SchoolAuthRecovery(), session(authenticated=False)
            if isinstance(outcome,Exception): school.login.side_effect=outcome
            else: school.login.side_effect=None; school.login.return_value=outcome
            operation = AsyncMock(return_value=[])
            for _ in range(3):
                with self.assertRaises(AuthenticationRequired) as caught:
                    await recovery.run(1,school,operation,lambda:("fixture","fixture"))
                self.assertNotIn("private-fixture",str(caught.exception))
            self.assertEqual(1,school.login.await_count); operation.assert_not_awaited()
            school.user={}; school.reauthentication_error=None  # Explicit successful login.
            self.assertEqual([],await recovery.run(1,school,operation,lambda:None))

    async def test_second_auth_failure_does_not_loop_and_throttles_following_sources(self):
        recovery, school = SchoolAuthRecovery(), session()
        operation = AsyncMock(side_effect=AuthenticationRequired("still expired"))
        for _ in range(2):
            with self.assertRaises(AuthenticationRequired):
                await recovery.run(1,school,operation,lambda:("fixture","fixture"))
        self.assertEqual(2,operation.await_count); self.assertEqual(1,school.login.await_count)
        self.assertFalse(school.authenticated)

    async def test_concurrent_sources_share_one_renewal(self):
        recovery, school = SchoolAuthRecovery(), session()
        first = AsyncMock(side_effect=[AuthenticationRequired("expired"), ["first"]])
        second = AsyncMock(return_value=["second"])
        results = await asyncio.gather(
            recovery.run(1,school,first,lambda:("fixture","fixture")),
            recovery.run(1,school,second,lambda:("fixture","fixture")))
        self.assertEqual([["first"],["second"]],results)
        self.assertEqual(1,school.login.await_count)

    async def test_users_and_routes_do_not_block_each_other(self):
        recovery = SchoolAuthRecovery()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set(); await release.wait(); return []
        pending = asyncio.create_task(recovery.run(1,session(),slow,lambda:None))
        try:
            await asyncio.wait_for(entered.wait(),1)
            for uid,mode in ((1,"webvpn"),(2,"direct")):
                self.assertEqual([],await asyncio.wait_for(recovery.run(uid,session(mode),
                    AsyncMock(return_value=[]),lambda:None),1))
        finally:
            release.set(); await pending

    async def test_aggregator_writes_only_recovered_result_and_preserves_old_on_failure(self):
        from app.db import Database
        from app.services.aggregator import Aggregator
        db=Database(":memory:"); db.init()
        uid=db.create_user("recovery@example.com","Fixture","hash")["id"]
        event={"source":"spoc","external_id":"old","kind":"assignment","title":"old"}
        db.upsert_event(uid,event)
        school=session()
        adapter=Mock(id="spoc",label="SPOC",last_error_count=0,last_error_detail="",cleanup_kinds=None)
        adapter.sync=AsyncMock(side_effect=[AuthenticationRequired("expired"),[event]])
        recovery=SchoolAuthRecovery()
        async def read(current):
            return await recovery.run(uid,school,current.sync,lambda:("fixture","fixture"))
        try:
            self.assertEqual({"spoc":1},await Aggregator(db,uid,[adapter],read_events=read).sync())
            self.assertEqual("healthy",db.get_source(uid,"spoc")["status"])
            adapter.sync.side_effect=AuthenticationRequired("expired")
            await Aggregator(db,uid,[adapter],read_events=read).sync()
            self.assertEqual("login_required",db.get_source(uid,"spoc")["status"])
            self.assertEqual(1,len(db.list_events(uid)))
        finally: db.close()

    async def test_main_sync_recovers_without_false_failure_notification(self):
        import app.main as main
        from app.db import Database
        for mode in ("direct","webvpn"):
            db=Database(":memory:"); db.init()
            uid=db.create_user("main@example.com","Fixture","hash")["id"]
            db.upsert_school_connection(uid,{"school_id":"fixture","status":"connected"})
            db.upsert_source(uid,{"id":"spoc","label":"SPOC","status":"healthy"})
            db.update_source_settings(uid,"spoc",network_mode=mode)
            school=session(mode)
            adapter=Mock(id="spoc",label="SPOC",last_error_count=0,last_error_detail="",cleanup_kinds=None)
            adapter.sync=AsyncMock(side_effect=[AuthenticationRequired("expired"),[]])
            sender=Mock(send=AsyncMock())
            try:
                with patch.object(main,"db",db), patch.object(main,"school_recovery",SchoolAuthRecovery()), \
                     patch.object(main,"source_session",AsyncMock(return_value=school)), \
                     patch.object(main,"remembered_school_credentials",return_value=("fixture","fixture")), \
                     patch.object(main,"SpocAdapter",Mock(return_value=adapter)), \
                     patch.object(main,"rule_engine",Mock()), patch.object(main,"notification_sender",sender), \
                     patch.dict(main.sync_progress,{(uid,"spoc"):{}},clear=True):
                    await main.sync_one(uid,"spoc")
                self.assertEqual("healthy",db.get_source(uid,"spoc")["status"])
                self.assertEqual(1,school.login.await_count)
                sender.send.assert_not_awaited()
            finally: db.close()

    async def test_manual_login_required_ui_does_not_invalidate_other_route(self):
        import app.main as main
        from app.db import Database
        from datetime import date
        for mode in ("direct","webvpn"):
            db=Database(":memory:"); db.init()
            uid=db.create_user("manual@example.com","Fixture","hash")["id"]
            db.upsert_school_connection(uid,{"school_id":"fixture","status":"connected"})
            db.upsert_source(uid,{"id":"iclass","label":"签到","status":"healthy"})
            school=session(mode)
            adapter=Mock(query_day=AsyncMock(side_effect=AuthenticationRequired("expired")))
            try:
                with patch.object(main,"db",db), patch.object(main,"school_recovery",SchoolAuthRecovery()), \
                     patch.object(main,"source_session",AsyncMock(return_value=school)), \
                     patch.object(main,"IClassAdapter",return_value=adapter):
                    with self.assertRaises(AuthenticationRequired):
                        await main.query_attendance_day(uid,date(2030,9,1))
                self.assertEqual("login_required",db.get_source(uid,"iclass")["status"])
                self.assertEqual("login_required" if mode=="direct" else "connected",db.school_connection(uid)["status"])
                school.login.assert_not_awaited()
            finally: db.close()
