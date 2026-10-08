import unittest

from app.upstream.sso import parse_captcha_id, parse_login_error, parse_login_form


class SsoParserTest(unittest.TestCase):
    def test_reads_hidden_form_fields(self):
        html = '''
        <form id="fm1"><input type="hidden" name="execution" value="e1s1">
        <input type="text" name="username"><input type="password" name="password">
        <input type="hidden" name="_eventId" value="submit"></form>
        '''
        self.assertEqual(
            {"execution": "e1s1", "username": "", "_eventId": "submit"},
            parse_login_form(html),
        )

    def test_reads_current_login_form(self):
        html = '''
        <form id="loginForm" action="login">
          <input type="text" name="username">
          <input type="password" name="password">
          <input name="type" value="username_password">
          <input name="execution" value="current-token">
          <input name="_eventId" value="submit">
        </form>
        '''
        self.assertEqual(
            {
                "username": "", "type": "username_password",
                "execution": "current-token", "_eventId": "submit",
            },
            parse_login_form(html),
        )

    def test_reads_captcha_configuration(self):
        html = '''<script>config.captcha = { type: "image", id: "abc-123" }</script>'''
        self.assertEqual("abc-123", parse_captcha_id(html))

    def test_reads_login_error(self):
        self.assertEqual("账号或密码错误", parse_login_error('<div class="tip-text">账号或密码错误</div>'))

    def test_reads_current_login_error(self):
        self.assertEqual(
            "用户名或密码错误",
            parse_login_error('<div id="errorDiv"><p>用户名或密码错误</p></div>'),
        )


if __name__ == "__main__":
    unittest.main()
