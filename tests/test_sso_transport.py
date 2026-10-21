import unittest
from urllib.parse import parse_qs

import httpx

from app.upstream.cas import secure_cas_transport_url
from app.upstream.sso import AuthenticationFlowError, BuaaSsoSession, SSO_URL, UC_LOGIN_URL, UC_STATUS_URL
from app.upstream.webvpn import WebVpnClient, from_webvpn_url, to_webvpn_url


FORM = '<form id="loginForm"><input name="execution" value="fixture"><input name="type" value="username_password"><input name="_eventId" value="submit"><input name="username"><input type="password" name="password"></form>'


class CasTransportTest(unittest.TestCase):
    def test_upgrades_only_the_exact_sso_default_http_origin_without_reencoding_queries(self):
        suffix = '/login?service=https%3A%2F%2Fuc.buaa.edu.cn%2Fapi%2Flogin%3Ftarget%3Da%252Bb&ticket=fixture'
        for prefix in ('http://sso.buaa.edu.cn', 'http://sso.buaa.edu.cn:80', 'HTTP://SSO.BUAA.EDU.CN:80'):
            self.assertEqual('https://sso.buaa.edu.cn' + suffix, secure_cas_transport_url(prefix + suffix))
        for url in ('https://sso.buaa.edu.cn' + suffix, 'http://sso.buaa.edu.cn:8080' + suffix,
                    'http://sso.buaa.edu.cn.evil.example' + suffix, 'http://sso.buaa.edu.cn@evil.example/login',
                    'http://judge.buaa.edu.cn/?service=http%3A%2F%2Fsso.buaa.edu.cn', '//sso.buaa.edu.cn/login'):
            self.assertEqual(url, secure_cas_transport_url(url))


class SsoTransportTest(unittest.IsolatedAsyncioTestCase):
    async def session(self, mode, handler):
        school = BuaaSsoSession(network_mode=mode)
        await school.client.aclose()
        transport = httpx.MockTransport(handler)
        school.client = WebVpnClient(transport=transport) if mode == 'webvpn' else httpx.AsyncClient(transport=transport, follow_redirects=True)
        self.addAsyncCleanup(school.close)
        return school

    async def test_complete_ticket_exchange_for_direct_and_gateway_with_raw_or_wrapped_http_location(self):
        for mode, wrapped in [('direct', False), ('webvpn', False), ('webvpn', True)]:
            for restored in (False, True):
                with self.subTest(mode=mode, wrapped=wrapped, restored=restored):
                    calls, password_posts = [], []
                    exchanged = False
                    service_query = 'service=https%3A%2F%2Fuc.buaa.edu.cn%2Fapi%2Flogin%3Ftarget%3Da%252Bb'
                    def handler(request):
                        nonlocal exchanged
                        calls.append(request)
                        self.assertEqual('https', request.url.scheme)
                        if mode == 'webvpn':
                            self.assertEqual('d.buaa.edu.cn', request.url.host)
                            self.assertTrue(request.url.path.startswith('/https/'))
                        upstream = httpx.URL(from_webvpn_url(str(request.url)))
                        self.assertEqual('https', upstream.scheme)
                        if upstream.host == 'sso.buaa.edu.cn':
                            if request.method == 'POST':
                                password_posts.append(request)
                                values = parse_qs(request.content.decode())
                                self.assertEqual(['fixture-user'], values['username'])
                                self.assertEqual(['fixture-password'], values['password'])
                                return httpx.Response(302, headers={'Location': 'https://uc.buaa.edu.cn/', 'Set-Cookie': 'TGC=fixture; Secure; Path=/'})
                            if upstream.query:
                                self.assertEqual(service_query, upstream.query.decode())
                                self.assertIn('TGC=fixture', request.headers.get('cookie', ''))
                                return httpx.Response(302, headers={'Location': UC_LOGIN_URL + '&ticket=fixture-ticket'})
                            return httpx.Response(302, headers={'Location': 'https://uc.buaa.edu.cn/'}) if restored else httpx.Response(200, text=FORM)
                        if upstream.path == '/api/login':
                            if upstream.params.get('ticket'):
                                exchanged = True
                                return httpx.Response(302, headers={'Location': UC_LOGIN_URL, 'Set-Cookie': 'JSESSIONID=fixture-uc; Secure; Path=/'})
                            if exchanged:
                                return httpx.Response(302, headers={'Location': '/'})
                            location = 'http://sso.buaa.edu.cn/login?' + service_query
                            if wrapped:
                                # Literal school gateway route, not the normalizer under test.
                                location = to_webvpn_url(location)
                            return httpx.Response(302, headers={'Location': location})
                        if upstream.path == '/api/uc/status':
                            valid = exchanged and 'JSESSIONID=fixture-uc' in request.headers.get('cookie', '')
                            return httpx.Response(200, json={'code': 0, 'data': {'schoolid': 'fixture-user'}} if valid else {'code': 10600, 'data': None})
                        # A UC landing page can be unavailable while its API is
                        # healthy. Only the final status confirms authentication.
                        return httpx.Response(404 if restored else 200, text='UC landing')
                    school = await self.session(mode, handler)
                    if restored:
                        domain = 'd.buaa.edu.cn' if mode == 'webvpn' else 'sso.buaa.edu.cn'
                        school.client.cookies.set('TGC', 'fixture', domain=domain, path='/')
                    result = await school.login('fixture-user', 'fixture-password')
                    self.assertTrue(result['authenticated'])
                    self.assertTrue(exchanged)
                    self.assertEqual(0 if restored else 1, len(password_posts))
                    self.assertEqual('fixture-user', school.username)
                    self.assertTrue(all(not request.content for request in calls if request.method == 'GET'))

    async def test_redirect_loops_stop_without_including_tickets_in_error(self):
        for mode in ('direct', 'webvpn'):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(302, headers={'Location': UC_LOGIN_URL + '&ticket=private-fixture'})
            school = await self.session(mode, handler)
            with self.assertRaises(AuthenticationFlowError) as caught:
                await school.preload()
            self.assertLessEqual(len(calls), 11)
            self.assertNotIn('private-fixture', str(caught.exception))
            self.assertFalse(school.authenticated)

    async def test_restored_other_account_is_not_reused_or_logged_out(self):
        calls=[]
        def handler(request):
            calls.append(request)
            if request.url.path=='/login':
                if 'TGC=old' in request.headers.get('cookie',''):
                    return httpx.Response(302,headers={'Location':'https://uc.buaa.edu.cn/'})
                if request.method=='POST':
                    return httpx.Response(302,headers={'Location':'https://uc.buaa.edu.cn/','Set-Cookie':'TGC=new; Secure; Path=/'})
                return httpx.Response(200,text=FORM)
            if request.url.path=='/api/uc/status':
                account='old-user' if school.client.cookies.get('TGC')=='old' else 'fixture-user'
                return httpx.Response(200,json={'code':0,'data':{'schoolid':account}})
            return httpx.Response(200,text='UC')
        school=await self.session('direct',handler)
        school.client.cookies.set('TGC','old',domain='sso.buaa.edu.cn',path='/')
        result=await school.login('fixture-user','fixture-password')
        self.assertEqual('fixture-user',result['user']['schoolid'])
        self.assertEqual(1,len([r for r in calls if r.method=='POST']))
        self.assertFalse(any('/logout' in r.url.path for r in calls))

    async def test_rejects_external_redirects_and_credential_replay_in_both_modes(self):
        for mode in ('direct', 'webvpn'):
            for status, target in [(302, 'https://evil.example/'), (307, 'https://uc.buaa.edu.cn/api/login'),
                                   (302, 'http://sso.buaa.edu.cn:8080/login'), (302, 'https://sso.buaa.edu.cn@evil.example/login')]:
                with self.subTest(mode=mode, status=status, target=target):
                    calls = []
                    def handler(request):
                        calls.append(request)
                        return httpx.Response(status, headers={'Location': target})
                    school = await self.session(mode, handler)
                    school._form = {'execution': 'fixture'}
                    with self.assertRaises(AuthenticationFlowError):
                        await school.login('fixture-user', 'fixture-password')
                    self.assertEqual(1, len(calls))

    async def test_uncertain_uc_status_is_not_a_password_error_and_never_sets_authenticated(self):
        for data in ({'code': 10600, 'data': None}, [], {'code': 0, 'data': None}):
            def handler(request):
                if request.url.path == '/api/uc/status':
                    return httpx.Response(200, json=data)
                return httpx.Response(200, text=FORM if request.method == 'GET' and request.url.path == '/login' else 'ok')
            school = await self.session('direct', handler)
            with self.assertRaises(AuthenticationFlowError):
                await school.login('fixture-user', 'fixture-password')
            self.assertFalse(school.authenticated)

    async def test_captcha_does_not_submit_password_and_credential_rejection_still_raises_login_failed(self):
        from app.upstream.sso import LoginFailed
        posts = []
        def handler(request):
            if request.method == 'POST':
                posts.append(request)
                return httpx.Response(200, text='<div class="tip-text">账号或密码错误</div>' + FORM)
            return httpx.Response(200, text=FORM + '<script>config.captcha = {type:"image",id:"fixture"}</script>')
        school = await self.session('direct', handler)
        result = await school.login('fixture-user', 'fixture-password')
        self.assertTrue(result['captcha_required'])
        self.assertEqual([], posts)
        with self.assertRaises(LoginFailed) as caught:
            await school.login('fixture-user', 'fixture-password', 'fixture-captcha')
        self.assertNotIsInstance(caught.exception, AuthenticationFlowError)
        self.assertEqual(1, len(posts))

    async def test_webvpn_transport_upgrades_sso_redirect_but_keeps_business_service_encoding(self):
        calls = []
        service = 'service=http%3A%2F%2Fjudge.buaa.edu.cn%2F'
        def handler(request):
            calls.append(request)
            upstream = httpx.URL(from_webvpn_url(str(request.url)))
            self.assertEqual('https', upstream.scheme)
            if len(calls) == 1:
                return httpx.Response(302, headers={'Location': 'http://sso.buaa.edu.cn/login?' + service})
            self.assertEqual(service, upstream.query.decode())
            return httpx.Response(200)
        school = await self.session('webvpn', handler)
        await school.client.get('https://uc.buaa.edu.cn/api/login')
        self.assertEqual(2, len(calls))


if __name__ == '__main__':
    unittest.main()
