import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.testclient import TestClient

import app.main as main
from app.adapters.judge import JudgeAdapter, SERVICE_LOGIN
from app.db import Database
from app.services.aggregator import Aggregator
from app.services.school_sessions import SchoolSessionManager
from app.upstream.sso import BuaaSsoSession, AuthenticationRequired, SSO_URL, UC_STATUS_URL
from app.upstream.webvpn import WebVpnClient, to_webvpn_url, from_webvpn_url, encode_host


class WebVpnUrlTest(unittest.TestCase):
    def test_official_sso_host_vector(self):
        self.assertEqual("77726476706e69737468656265737421e3e44ed225256951300d8db9d6562d", encode_host("sso.buaa.edu.cn"))

    def test_roundtrip_paths_query_fragment_and_ports(self):
        for url in ["https://judge.buaa.edu.cn/", "http://judge.buaa.edu.cn/assignment/index.jsp?assignID=1",
                    "https://sso.buaa.edu.cn/login?service=http%3A%2F%2Fjudge.buaa.edu.cn%2F",
                    "https://uc.buaa.edu.cn//web/?a=1&a=2#/user?type=test",
                    "http://judge.buaa.edu.cn:443/a/", "https://judge.buaa.edu.cn:80/a/"]:
            encoded = to_webvpn_url(url)
            self.assertEqual(url, from_webvpn_url(encoded))
            self.assertEqual(encoded, to_webvpn_url(encoded))

    def test_rejects_non_school_or_insecure_gateway_targets(self):
        for url in ["http://d.buaa.edu.cn/login", "https://evil.example/", "https://judge.buaa.edu.cn.evil.example/",
                    "https://user:password@judge.buaa.edu.cn/", "https://judge.buaa.edu.cn:8347/",
                    "https://127.0.0.1/", "file:///etc/passwd"]:
            with self.assertRaises(ValueError):
                to_webvpn_url(url)
        for host in ["evil.example", "127.0.0.1"]:
            with self.assertRaises(ValueError):
                to_webvpn_url("https://d.buaa.edu.cn/https/" + encode_host(host) + "/")

    def test_native_gateway_and_proxied_auth_detection(self):
        self.assertEqual("https://d.buaa.edu.cn/login?cas_login=true", to_webvpn_url("https://d.buaa.edu.cn/login?cas_login=true"))
        for url in [to_webvpn_url(SSO_URL), "https://d.buaa.edu.cn/login"]:
            with self.assertRaises(AuthenticationRequired):
                JudgeAdapter._ensure_html(httpx.Response(200, text="login", request=httpx.Request("GET", url)), "JUDGE")

    def test_unwraps_encoded_gateway_callback_without_losing_ticket(self):
        callback = "https://d.buaa.edu.cn/login?cas_login=true&ticket=fixture-ticket"
        prefix = "https://d.buaa.edu.cn/https/" + encode_host("d.buaa.edu.cn")
        wrapped = prefix + "/login?cas_login=true&ticket=fixture-ticket"
        self.assertEqual(callback, from_webvpn_url(wrapped))
        self.assertEqual(callback, to_webvpn_url(wrapped))
        self.assertEqual(callback, to_webvpn_url(prefix + wrapped.removeprefix("https://d.buaa.edu.cn")))

    def test_wrapped_gateway_does_not_bypass_destination_or_tls_validation(self):
        prefix = "https://d.buaa.edu.cn/https/" + encode_host("d.buaa.edu.cn")
        for target in [prefix + "/https/" + encode_host("evil.example") + "/",
                       "https://d.buaa.edu.cn/http/" + encode_host("d.buaa.edu.cn") + "/login"]:
            with self.assertRaises(ValueError):
                to_webvpn_url(target)

    def test_rejected_route_errors_are_specific_and_never_include_ticket(self):
        for payload, protocol, reason in [
            (encode_host("unsupported.buaa.edu.cn"), "https", "目标主机未支持：unsupported.buaa.edu.cn"),
            (encode_host("judge.buaa.edu.cn"), "https-8080", "端口未支持"),
            ("invalid-payload", "https", "主机编码无法解析"),
        ]:
            with self.assertRaises(ValueError) as caught:
                from_webvpn_url(f"https://d.buaa.edu.cn/{protocol}/{payload}/?ticket=private-fixture")
            self.assertIn(reason, str(caught.exception))
            self.assertNotIn("private-fixture", str(caught.exception))

    def test_timeouts_report_only_host_not_ticket_or_account(self):
        exc = httpx.ConnectTimeout("", request=httpx.Request("GET", "https://judge.buaa.edu.cn/?ticket=private-fixture"))
        text = Aggregator._error_detail(exc)
        self.assertIn("judge.buaa.edu.cn", text)
        self.assertIn("WebVPN", text)
        self.assertNotIn("private-fixture", text)
        self.assertIn("连接", Aggregator._error_detail(httpx.ConnectTimeout("")))


class WebVpnTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_cas_encoded_gateway_callback_is_followed_as_native_https_get(self):
        requests = []
        callback = "https://d.buaa.edu.cn/https/" + encode_host("d.buaa.edu.cn") + "/login?cas_login=true&ticket=fixture-ticket"
        def handler(request):
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(302, headers={"Location":callback})
            if request.url.path == "/login":
                self.assertEqual("fixture-ticket", request.url.params["ticket"])
                return httpx.Response(302, headers={"Location":"/"})
            return httpx.Response(200, text="gateway portal")
        client = WebVpnClient(transport=httpx.MockTransport(handler))
        try:
            response = await client.post(SSO_URL, data={"password":"not-a-real-password"})
            self.assertEqual(200, response.status_code)
            self.assertEqual(["POST", "GET", "GET"], [r.method for r in requests])
            self.assertEqual("/login", requests[1].url.path)
            self.assertTrue(all(r.url.host == "d.buaa.edu.cn" and r.url.scheme == "https" for r in requests))
            self.assertTrue(all(not r.content for r in requests[1:]))
        finally:
            await client.aclose()

    async def test_judge_sync_chooses_vpn_session_without_direct_login(self):
        db = Database(":memory:"); db.init()
        user = db.create_user("route@example.com", "Route", "hash")
        db.upsert_source(user["id"], {"id":"judge","label":"JUDGE","status":"not_connected"})
        db.update_source_settings(user["id"], "judge", network_mode="webvpn")
        manager = Mock()
        session = Mock(authenticated=True, username="12345678")
        manager.get_webvpn = AsyncMock(return_value=session)
        manager.get = AsyncMock()
        adapter = Mock(id="judge",label="JUDGE",last_error_count=0,last_error_detail="",cleanup_kinds=None)
        adapter.sync = AsyncMock(return_value=[])
        adapter_class = Mock(return_value=adapter); adapter_class.label="JUDGE"
        sender = Mock(send=AsyncMock())
        try:
            with patch.object(main,"db",db), patch.object(main,"school_sessions",manager), \
                 patch.object(main,"JudgeAdapter",adapter_class), patch.object(main,"rule_engine",Mock()), \
                 patch.object(main,"notification_sender",sender), patch.dict(main.sync_locks,{},clear=True), \
                 patch.dict(main.sync_progress,{(user["id"],"judge"):{}},clear=True):
                await main.sync_one(user["id"], "judge")
            manager.get_webvpn.assert_awaited_once_with(user["id"])
            manager.get.assert_not_awaited()
            self.assertIs(session, adapter_class.call_args.args[0])
            self.assertEqual("webvpn", db.get_source(user["id"],"judge")["network_mode"])
        finally:
            db.close()

    async def test_missing_vpn_login_preserves_primary_and_existing_data(self):
        db = Database(":memory:"); db.init()
        user = db.create_user("missing@example.com", "Missing", "hash")
        db.upsert_school_connection(user["id"], {"school_id":"12345678","status":"connected"})
        db.upsert_source(user["id"], {"id":"judge","label":"JUDGE","status":"healthy","event_count":1})
        db.upsert_event(user["id"], {"source":"judge","external_id":"1","kind":"assignment","title":"任务"})
        db.update_source_settings(user["id"],"judge",network_mode="webvpn")
        manager = Mock()
        manager.get_webvpn = AsyncMock(return_value=Mock(authenticated=False))
        manager.get = AsyncMock()
        try:
            with patch.object(main,"db",db), patch.object(main,"school_sessions",manager), \
                 patch.dict(main.sync_locks,{},clear=True), patch.dict(main.sync_progress,{(user["id"],"judge"):{}},clear=True):
                await main.sync_one(user["id"], "judge")
            self.assertEqual("login_required", db.get_source(user["id"],"judge")["status"])
            self.assertEqual("connected", db.school_connection(user["id"])["status"])
            self.assertEqual(1, len(db.list_events(user["id"])))
            manager.get.assert_not_awaited()
        finally:
            db.close()

    async def test_normalizes_cas_absolute_and_relative_redirects(self):
        requests = []
        def handler(request):
            requests.append(request)
            original = httpx.URL(from_webvpn_url(str(request.url)))
            if original.host == "sso.buaa.edu.cn":
                return httpx.Response(302, headers={"Location":"http://judge.buaa.edu.cn/?ticket=fixture"})
            if original.path == "/":
                return httpx.Response(302, headers={"Location":"/indexcs/simple.jsp"})
            return httpx.Response(200, text="JUDGE")
        client = WebVpnClient(transport=httpx.MockTransport(handler))
        try:
            response = await client.get(SERVICE_LOGIN)
            self.assertEqual(200, response.status_code)
            self.assertEqual(2, len(response.history))
            self.assertTrue(all(r.url.host == "d.buaa.edu.cn" and r.url.scheme == "https" for r in requests))
            self.assertEqual("/indexcs/simple.jsp", httpx.URL(from_webvpn_url(str(response.url))).path)
        finally:
            await client.aclose()

    async def test_credential_post_is_not_replayed_after_302(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(302, headers={"Location":"https://d.buaa.edu.cn/login?cas_login=true"})
            return httpx.Response(200)
        client = WebVpnClient(transport=httpx.MockTransport(handler))
        try:
            await client.post(SSO_URL, data={"username":"fixture", "password":"not-a-real-password"})
            self.assertEqual(["POST", "GET"], [r.method for r in requests])
            self.assertFalse(requests[1].content)
        finally:
            await client.aclose()

    async def test_blocks_credential_replay_and_external_redirect(self):
        for status, target in [(307, "https://d.buaa.edu.cn/login"), (302, "https://evil.example/")]:
            requests = []
            def handler(request):
                requests.append(request)
                return httpx.Response(status, headers={"Location":target})
            client = WebVpnClient(transport=httpx.MockTransport(handler))
            try:
                with self.assertRaises(ValueError):
                    await client.post(SSO_URL, data={"password":"not-a-real-password"})
                self.assertEqual(1, len(requests))
            finally:
                await client.aclose()

    async def test_disallows_business_writes(self):
        handler = Mock(return_value=httpx.Response(200))
        client = WebVpnClient(transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(ValueError):
                await client.post("https://judge.buaa.edu.cn/assignment/index.jsp", data={"submit":True})
            handler.assert_not_called()
        finally:
            await client.aclose()

    async def test_login_uses_gateway_final_form_and_cookie_bound_captcha(self):
        requests = []
        form_url = to_webvpn_url(SSO_URL + "?service=https%3A%2F%2Fd.buaa.edu.cn%2Flogin%3Fcas_login%3Dtrue")
        def handler(request):
            requests.append(request)
            original = httpx.URL(from_webvpn_url(str(request.url)))
            if original.path == "/captcha":
                return httpx.Response(200, content=b"fixture-captcha", headers={"Content-Type":"image/png"})
            if original.host == "uc.buaa.edu.cn":
                if original.path == "/api/uc/status":
                    return httpx.Response(200, json={"code":0, "data":{"name":"Fixture"}})
                return httpx.Response(200)
            if request.method == "POST":
                self.assertEqual(form_url, str(request.url))
                return httpx.Response(302, headers={"Location":"https://d.buaa.edu.cn/https/" + encode_host("d.buaa.edu.cn") + "/"})
            if request.url.path == "/":
                return httpx.Response(200, text="gateway portal")
            if str(request.url) != form_url:
                return httpx.Response(302, headers={"Location":form_url})
            return httpx.Response(200, text='<form id="loginForm"><input name="execution" value="fixture"><input name="username"><input type="password" name="password"></form><script>config.captcha = { type: "image", id: "fixture-captcha" }</script>')
        session = BuaaSsoSession(network_mode="webvpn")
        await session.client.aclose()
        session.client = WebVpnClient(transport=httpx.MockTransport(handler))
        try:
            pre = await session.preload()
            self.assertTrue(pre["captcha_required"])
            missing = await session.login("fixture", "not-a-real-password")
            self.assertFalse(missing["authenticated"])
            self.assertFalse(any(r.method == "POST" for r in requests))
            image, mime = await session.captcha("fixture-captcha")
            self.assertEqual((b"fixture-captcha", "image/png"), (image, mime))
            authenticated = await session.login("fixture", "not-a-real-password", captcha="fixture-solution")
            self.assertTrue(authenticated["authenticated"])
            self.assertEqual("fixture", session.username)
            self.assertTrue(all(r.url.host == "d.buaa.edu.cn" for r in requests))
        finally:
            await session.close()

    async def test_complete_judge_sync_only_uses_school_gateway_gets(self):
        requests = []
        def handler(request):
            requests.append(request)
            original = httpx.URL(from_webvpn_url(str(request.url)))
            if original.host == "sso.buaa.edu.cn":
                return httpx.Response(302, headers={"Location":"http://judge.buaa.edu.cn/?ticket=fixture"})
            if original.path == "/courselist.jsp" and original.params.get("courseID") == "0":
                return httpx.Response(200, text='<a href="courselist.jsp?courseID=1">课程甲</a>')
            if original.params.get("assignID"):
                return httpx.Response(200, text='作业时间：2030-01-01 00:00 至 2030-10-30 22:00 作业类型：练习 共 1 道 未提交')
            if original.path == "/assignment/index.jsp":
                return httpx.Response(200, text='<a href="index.jsp?assignID=1">作业甲</a>')
            return httpx.Response(200, text="JUDGE")
        client = WebVpnClient(transport=httpx.MockTransport(handler))
        try:
            session = Mock(authenticated=True, client=client)
            events = await JudgeAdapter(session).sync()
            self.assertEqual(1, len(events))
            self.assertEqual("课程甲 · 作业甲", events[0]["title"])
            self.assertEqual("2030-10-30T22:00:00+08:00", events[0]["due_at"])
            self.assertTrue(all(r.url.host == "d.buaa.edu.cn" and r.method == "GET" for r in requests))
        finally:
            await client.aclose()

    async def test_sessions_are_isolated_by_user_and_network(self):
        manager = SchoolSessionManager()
        try:
            direct = await manager.get(1)
            vpn = await manager.get_webvpn(1)
            other = await manager.get_webvpn(2)
            vpn.client.cookies.set("fixture", "cookie", domain="d.buaa.edu.cn")
            self.assertIs(vpn, await manager.get_webvpn(1))
            self.assertFalse(direct.client.cookies)
            self.assertFalse(other.client.cookies)
            self.assertFalse(manager.webvpn_authenticated(1))
            vpn.user = {"name":"Fixture"}
            self.assertTrue(manager.webvpn_authenticated(1))
            await manager.reset_webvpn(1)
            self.assertFalse(manager.webvpn_authenticated(1))
        finally:
            await manager.close()


class WebVpnSettingsApiTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:"); self.db.init()
        self.user = self.db.create_user("vpn@example.com", "Fixture", "hash")
        self.other = self.db.create_user("other@example.com", "Other", "hash")
        self.manager = Mock()
        self.vpn = Mock()
        self.vpn.preload = AsyncMock(return_value={"captcha_required":True,"captcha_id":"fixture"})
        self.vpn.captcha = AsyncMock(return_value=(b"fixture", "image/png"))
        self.vpn.login = AsyncMock(return_value={"authenticated":True,"user":{"name":"Fixture"}})
        self.manager.get_webvpn = AsyncMock(return_value=self.vpn)
        self.manager.get = AsyncMock()
        self.manager.webvpn_authenticated.return_value = False
        self.patches = [patch.object(main, "db", self.db), patch.object(main, "school_sessions", self.manager),
                        patch.object(main, "start_sync", return_value={})]
        for p in self.patches: p.start()
        main.app.dependency_overrides[main.csrf_user] = lambda: self.user
        main.app.dependency_overrides[main.current_user] = lambda: self.user
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close(); main.app.dependency_overrides.clear()
        for p in reversed(self.patches): p.stop()
        self.db.close()

    def test_mode_can_be_saved_before_school_login_and_is_tenant_scoped(self):
        response = self.client.patch("/api/sources/judge", json={"network_mode":"webvpn"})
        self.assertEqual(200, response.status_code)
        self.assertEqual("webvpn", response.json()["network_mode"])
        self.assertIsNone(self.db.get_source(self.other["id"], "judge"))
        sources = {s["id"]:s for s in self.client.get("/api/dashboard").json()["sources"]}
        self.assertEqual("webvpn", sources["judge"]["network_mode"])
        self.assertTrue(sources["judge"]["webvpn_login_required"])
        self.assertEqual("direct", sources["spoc"]["network_mode"])

    def test_sync_upsert_preserves_mode_and_events(self):
        self.db.upsert_source(self.user["id"], {"id":"judge", "label":"JUDGE", "status":"healthy", "event_count":1})
        self.db.upsert_event(self.user["id"], {"source":"judge","external_id":"1","title":"任务","kind":"assignment"})
        self.db.update_source_settings(self.user["id"], "judge", network_mode="webvpn")
        self.db.upsert_source(self.user["id"], {"id":"judge", "label":"JUDGE", "status":"healthy", "event_count":1})
        self.assertEqual("webvpn", self.db.get_source(self.user["id"], "judge")["network_mode"])
        self.assertEqual(1, len(self.db.list_events(self.user["id"])))

    def test_additive_old_schema_migration_defaults_to_direct(self):
        legacy = Database(":memory:")
        try:
            legacy._connection.execute("""CREATE TABLE user_sources (
                user_id INTEGER NOT NULL,id TEXT NOT NULL,label TEXT NOT NULL,status TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',last_sync_at TEXT,event_count INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,refresh_interval_minutes INTEGER NOT NULL DEFAULT 60,
                PRIMARY KEY(user_id,id))""")
            legacy._connection.execute("INSERT INTO user_sources(user_id,id,label,status,event_count) VALUES(1,'judge','JUDGE','healthy',7)")
            legacy.init()
            source = legacy.get_source(1,"judge")
            self.assertEqual("direct", source["network_mode"])
            self.assertEqual(7, source["event_count"])
        finally:
            legacy.close()

    def test_rejects_invalid_or_other_source_modes(self):
        self.assertEqual(422, self.client.patch("/api/sources/judge", json={"network_mode":"proxy"}).status_code)
        self.assertEqual(422, self.client.patch("/api/sources/spoc", json={"network_mode":"webvpn"}).status_code)
        self.assertEqual(403, self.client.patch("/api/sources/iclass", json={"network_mode":"webvpn"}).status_code)

    def test_mode_change_is_blocked_during_sync(self):
        job = Mock(); job.done.return_value = False
        with patch.dict(main.sync_jobs, {(self.user["id"], "judge"):job}, clear=True):
            response = self.client.patch("/api/sources/judge", json={"network_mode":"webvpn"})
            self.assertEqual(409, response.status_code)
        self.assertIsNone(self.db.get_source(self.user["id"], "judge"))

    def test_preload_and_captcha_use_selected_mode(self):
        response = self.client.post("/api/school/preload?network_mode=webvpn")
        self.assertTrue(response.json()["captcha_required"])
        self.manager.get_webvpn.assert_awaited_once_with(self.user["id"])
        self.manager.get.assert_not_awaited()
        response = self.client.get("/api/school/captcha/fixture?network_mode=webvpn")
        self.assertEqual(b"fixture", response.content)
        self.vpn.captcha.assert_awaited_once_with("fixture")
        self.assertEqual(422, self.client.post("/api/school/preload?network_mode=evil").status_code)

    def prepare_school(self):
        self.db.upsert_school_connection(self.user["id"], {"school_id":"12345678", "display_name":"Fixture", "status":"connected", "password_ciphertext":"fixture-ciphertext"})
        self.client.patch("/api/sources/judge", json={"network_mode":"webvpn"})

    def test_webvpn_login_keeps_primary_school_connection_and_only_syncs_judge(self):
        self.prepare_school()
        response = self.client.post("/api/school/login", json={"username":"12345678", "password":"not-a-real-password", "network_mode":"webvpn"})
        self.assertEqual(200, response.status_code)
        self.assertEqual("webvpn", response.json()["network_mode"])
        self.assertEqual("fixture-ciphertext", self.db.school_secret(self.user["id"])["password_ciphertext"])
        main.start_sync.assert_called_once_with(self.user["id"], {"judge"})
        self.manager.get.assert_not_awaited()

    def test_wrong_school_identity_does_not_send_credentials(self):
        self.prepare_school()
        response = self.client.post("/api/school/login", json={"username":"87654321", "password":"not-a-real-password", "network_mode":"webvpn"})
        self.assertEqual(422, response.status_code)
        self.vpn.login.assert_not_awaited()

    def test_webvpn_captcha_keeps_primary_login_and_does_not_sync(self):
        self.prepare_school()
        self.vpn.login.return_value = {"authenticated":False,"captcha_required":True,"captcha_id":"fixture"}
        response = self.client.post("/api/school/login", json={"username":"12345678", "password":"not-a-real-password", "network_mode":"webvpn"})
        self.assertTrue(response.json()["captcha_required"])
        self.assertEqual("connected", self.db.school_connection(self.user["id"])["status"])
        main.start_sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
