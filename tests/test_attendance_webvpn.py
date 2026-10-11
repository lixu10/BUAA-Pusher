import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.adapters.byxt import ByxtAdapter, PORTAL_VERIFY
from app.adapters.iclass import IClassAdapter
from app.adapters.spoc import SpocAdapter, BASE as SPOC_BASE
from app.upstream.webvpn import WebVpnClient, HOST_PORTS, READ_POST_PATHS, from_webvpn_url, to_webvpn_url


class AttendanceWebVpnTest(unittest.IsolatedAsyncioTestCase):
    def test_failure_diagnostics_keep_status_and_date_without_private_payload(self):
        for payload in ({"STATUS":2,"result":[]},
                        {"STATUS":3,"result":[],"ERRMSG":"会话失效 private-fixture"},
                        {"STATUS":"private-fixture","result":{"sessionId":"private-fixture"}}):
            with self.assertLogs("app.adapters.iclass",level="WARNING") as logs:
                with self.assertRaises(Exception) as caught:
                    IClassAdapter._query_error(date(2030,9,1),payload)
            text=str(caught.exception)+" ".join(logs.output)
            self.assertIn("2030-09-01",text)
            self.assertIn("STATUS=",text)
            self.assertNotIn("private-fixture",text)

    async def test_all_school_hosts_and_query_posts_have_gateway_routes(self):
        seen=[]
        def handle(request): seen.append(request); return httpx.Response(200,json={})
        async with WebVpnClient(transport=httpx.MockTransport(handle)) as client:
            for host,ports in HOST_PORTS.items():
                port=8346 if host=="iclass.buaa.edu.cn" else 443
                native=f"https://{host}:{port}/" if port!=443 else f"https://{host}/"
                self.assertEqual(native,from_webvpn_url(to_webvpn_url(native)))
                await client.get(native)
            for host,paths in READ_POST_PATHS.items():
                for path in paths: await client.post(f"https://{host}{path}",json={})
        self.assertTrue(all(r.url.host=="d.buaa.edu.cn" and r.url.scheme=="https" for r in seen))

    async def test_blocks_checkin_and_time_calls_even_through_redirects(self):
        for endpoint in ("stu_scan_sign.action","get_server_time.action"):
            seen=[]
            def handle(request):
                seen.append(request)
                return httpx.Response(302,headers={"location":f"/app/course/{endpoint}"})
            async with WebVpnClient(transport=httpx.MockTransport(handle)) as client:
                for method in ("GET","POST"):
                    with self.assertRaises(ValueError):
                        await client.request(method,f"https://iclass.buaa.edu.cn:8347/app/course/{endpoint}")
                self.assertEqual([],seen)
                with self.assertRaises(ValueError):
                    await client.get("https://iclass.buaa.edu.cn:8347/app/course/get_stu_course_sched.action")
                self.assertEqual(1,len(seen))

    async def test_no_follow_redirect_preserves_login_name_and_business_headers_do_not_leak(self):
        seen=[]
        def handle(request):
            seen.append(request)
            native=httpx.URL(from_webvpn_url(str(request.url)))
            if native.host=="iclass.buaa.edu.cn":
                return httpx.Response(302,headers={"location":"https://sso.buaa.edu.cn/login?loginName=a%2Bb"})
            return httpx.Response(200)
        async with WebVpnClient(transport=httpx.MockTransport(handle)) as client:
            response=await client.get("https://iclass.buaa.edu.cn:8346/",follow_redirects=False)
            self.assertEqual(302,response.status_code); self.assertEqual(1,len(seen))
            await client.get("https://iclass.buaa.edu.cn:8346/",headers={"sessionId":"fixture","Token":"fixture"})
            self.assertNotIn("sessionId",seen[-1].headers); self.assertNotIn("Token",seen[-1].headers)

    async def test_full_iclass_flow_only_queries_status_and_parses_literal_plus(self):
        seen=[]
        def handle(request):
            seen.append(request)
            native=httpx.URL(from_webvpn_url(str(request.url)))
            if native.port==8346:
                target=to_webvpn_url("https://iclass.buaa.edu.cn:8346/?loginName=a%2Bb")
                return httpx.Response(302,headers={"location":httpx.URL(target).raw_path.decode()})
            if native.path.endswith("login.action"):
                self.assertEqual("a+b",native.params["phone"])
                return httpx.Response(200,json={"STATUS":"0","result":{"id":"fixture","sessionId":"fixture-session"}})
            self.assertEqual("fixture-session",request.headers["sessionId"])
            return httpx.Response(200,json={"STATUS":0,"result":[
                {"id":"1","courseName":"甲","classBeginTime":"10:00","classEndTime":"11:00","signStatus":"1"},
                {"id":"2","courseName":"乙","classBeginTime":"14:00","classEndTime":"15:00","signStatus":0}]})
        parent=WebVpnClient(transport=httpx.MockTransport(handle))
        session=SimpleNamespace(authenticated=True,network_mode="webvpn",client=parent)
        def child(): return WebVpnClient(transport=httpx.MockTransport(handle))
        try:
            with patch("app.adapters.iclass.WebVpnClient",side_effect=child):
                rows=await IClassAdapter(session).query_day(date(2030,9,1))
            self.assertEqual(["done","missing"],[r["status"] for r in rows])
            self.assertEqual(3,len(seen)); self.assertTrue(all(r.method=="GET" for r in seen))
            self.assertTrue(all(r.url.host=="d.buaa.edu.cn" for r in seen))
        finally: await parent.aclose()

    async def test_course_cas_exchange_accepts_gateway_url(self):
        def handle(request):
            native=httpx.URL(from_webvpn_url(str(request.url)))
            if native.host=="sso.buaa.edu.cn": return httpx.Response(302,headers={"location":PORTAL_VERIFY})
            return httpx.Response(200,json={"code":"0"})
        async with WebVpnClient(transport=httpx.MockTransport(handle)) as client:
            await ByxtAdapter(SimpleNamespace(authenticated=True,client=client))._ensure_login()

    async def test_spoc_relative_token_callback_and_read_post(self):
        seen=[]
        def handle(request):
            seen.append(request); native=httpx.URL(from_webvpn_url(str(request.url)))
            if native.path=="/spocnewht/cas": return httpx.Response(302,headers={"location":"/spocnew/cas?token=fixture-token"})
            self.assertEqual(SPOC_BASE+"/sys/casLogin",str(native))
            return httpx.Response(200,json={"code":200,"content":{"jsdm":"1"}})
        async with WebVpnClient(transport=httpx.MockTransport(handle)) as client:
            adapter=SpocAdapter(SimpleNamespace(client=client))
            await adapter._login(); self.assertEqual("fixture-token",adapter.token)
        self.assertEqual(["GET","POST"],[r.method for r in seen])


if __name__=="__main__": unittest.main()
