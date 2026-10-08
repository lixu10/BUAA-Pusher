import unittest
from datetime import date, datetime, timedelta, timezone

from app.adapters.grade import GradeAdapter
from app.adapters.bykc import BykcAdapter
from app.adapters.cgyy import CgyyAdapter
from app.adapters.iclass import IClassAdapter
from app.adapters.judge import JudgeAdapter
from app.adapters.libbook import LibBookAdapter
from app.adapters.spoc import SpocAdapter
from app.adapters.ygdk import YgdkAdapter


class SchoolAdapterParserTest(unittest.TestCase):
    def test_bykc_crypto_semester_and_event_mapping(self):
        encrypted = BykcAdapter.encrypt_request('{"hello":"博雅"}')
        self.assertEqual(
            '{"hello":"博雅"}',
            BykcAdapter._aes_decrypt(__import__("base64").b64decode(encrypted.body), encrypted.aes_key).decode(),
        )
        chosen = BykcAdapter.choose_semester([
            {"id": 1, "semesterEndDate": "2029-01-01 00:00:00"},
            {"id": 2, "semesterStartDate": "2030-09-01 00:00:00", "semesterEndDate": "2031-01-20 23:59:59"},
        ], date(2030, 9, 19))
        self.assertEqual(2, chosen["id"])
        adapter = BykcAdapter(object())
        events = adapter._events({
            "id": 10, "checkin": 0, "pass": 0,
            "courseInfo": {
                "id": 20, "courseName": "科学史", "coursePosition": "主楼 101",
                "courseStartDate": "2030-09-19 14:00:00", "courseEndDate": "2030-09-19 16:00:00",
                "courseSignType": 2,
                "courseSignConfig": '{"signStartDate":"2030-09-19 13:45:00","signEndDate":"2030-09-19 14:10:00","signOutStartDate":"2030-09-19 15:50:00","signOutEndDate":"2030-09-19 16:10:00"}',
            },
        })
        self.assertEqual(["boya", "signin", "signout"], [event["kind"] for event in events])
        self.assertEqual("missing", events[1]["status"])
        self.assertEqual("missing", events[2]["status"])

    def test_cgyy_signer_and_order_mapping(self):
        timestamp = 1710000000000
        clean = CgyyAdapter.sign("/api/test", {"a": "1", "b": "2"}, timestamp)
        noisy = CgyyAdapter.sign(
            "/api/test", {"b": "2", "a": "1", "empty": "", "none": None, "id": 123}, timestamp
        )
        self.assertEqual(clean, noisy)
        adapter = CgyyAdapter(object())
        event = adapter._event({
            "id": 7, "campusName": "学院路", "venueName": "体育馆", "siteName": "羽毛球 1",
            "reservationStartDate": "2030-09-19 18:00:00",
            "reservationEndDate": "2030-09-19 19:00:00", "orderStatus": 1, "checkStatus": 1,
        })
        self.assertEqual("venue", event["kind"])
        self.assertEqual("confirmed", event["status"])
        self.assertTrue(event["starts_at"].startswith("2030-09-19T18:00:00"))

    def test_grade_term_and_event_mapping(self):
        self.assertEqual(("2025-2026", 1), GradeAdapter._term_parts("2025-2026-1"))
        adapter = GradeAdapter(object())
        event = adapter._event("2025-2026-1", "row", {
            "kcmc": "软件工程", "kch": "B3J071010", "kccj": 95, "xf": 3,
        })
        self.assertEqual("grade", event["kind"])
        self.assertEqual("95", event["metadata"]["score"])
        self.assertTrue(event["_change_event"])

    def test_iclass_redirect_and_status_mapping(self):
        self.assertEqual(
            "student-id",
            IClassAdapter._login_name("https://iclass.example/path?type=x&loginName=student-id"),
        )
        adapter = IClassAdapter(object())
        event = adapter._event(date(2030, 9, 19), {
            "id": "schedule-1", "courseName": "软件工程",
            "classBeginTime": "08:00", "classEndTime": "09:35", "signStatus": "0",
        })
        self.assertEqual("signin", event["kind"])
        self.assertEqual("missing", event["status"])
        self.assertFalse(event["metadata"]["signed"])
        self.assertTrue(event["starts_at"].startswith("2030-09-19T08:00"))

    def test_spoc_protocol_helpers_and_assignment_mapping(self):
        self.assertEqual(
            "abc+123",
            SpocAdapter.extract_token("https://spoc.buaa.edu.cn/spocnew/cas?token=abc%2B123"),
        )
        self.assertEqual("Bk5c6bkwSqWGP0pHlrhIdg==", SpocAdapter.encrypt_param("{}"))
        adapter = SpocAdapter(object())
        event = adapter._event("2025-2026-1", {
            "zyid": "A1", "zymc": "第一次作业", "kcmc": "软件工程",
            "zyjzsj": "2030-09-19 23:59:00", "tjzt": "0", "mf": None,
        })
        self.assertEqual("assignment", event["kind"])
        self.assertEqual("pending", event["status"])
        self.assertFalse(event["metadata"]["submitted"])
        self.assertIsNone(event["starts_at"])
        self.assertTrue(event["due_at"].startswith("2030-09-19T23:59:00"))

    def test_judge_html_links_and_detail_mapping(self):
        courses = JudgeAdapter.parse_links(
            '<a href="courselist.jsp?courseID=0">全部</a><a href="courselist.jsp?courseID=12">算法</a>',
            "courseID", exclude="0",
        )
        self.assertEqual([("12", "算法")], courses)
        adapter = JudgeAdapter(object())
        event = adapter._event("12", "算法", "34", "第一次作业", '''
            <html><body>作业时间： 2030-09-01 08:00 至 2030-09-19 23:59:00
            作业满分：100 总分：95 最后一次提交时间 2030-09-18</body></html>
        ''')
        self.assertEqual("done", event["status"])
        self.assertEqual("95", event["metadata"]["score"])
        self.assertIsNone(event["starts_at"])
        self.assertTrue(event["metadata"]["opens_at"].startswith("2030-09-01T08:00:00"))
        self.assertTrue(event["due_at"].startswith("2030-09-19T23:59:00"))

    def test_ygdk_login_helpers_and_progress_mapping(self):
        self.assertEqual(
            "oauth-code",
            YgdkAdapter.extract_code("https://ygdk.buaa.edu.cn/#/home?code=oauth-code&state=x"),
        )
        chosen = YgdkAdapter.choose_classify([
            {"classify_id": 2, "name": "其他"},
            {"classify_id": "8", "name": "阳光体育", "term_num": "20"},
        ])
        adapter = YgdkAdapter(object())
        event = adapter._event(
            chosen,
            {"term_good_count_show": "12", "week_count": 2},
            {"term_id": 9, "name": "2026 秋"},
            12,
            20,
            9,
            "2030-09-01T00:00:00+08:00",
            "2030-12-31T23:59:59+08:00",
            now=datetime(2030, 12, 9, 9, tzinfo=timezone(timedelta(hours=8))),
        )
        self.assertEqual("fitness", event["kind"])
        self.assertEqual(8, event["metadata"]["remaining"])
        self.assertEqual("pending", event["status"])
        self.assertTrue(event["starts_at"].startswith("2030-08-26T00:00:00"))
        self.assertFalse(event["metadata"]["must_max_every_week"])

    def test_ygdk_only_flags_when_every_remaining_week_must_be_full(self):
        china = timezone(timedelta(hours=8))
        risk = YgdkAdapter._weekly_risk(
            current=8, target=16, week_count=0, week_target=4,
            due_at="2030-12-22T23:59:59+08:00",
            now=datetime(2030, 12, 9, 9, tzinfo=china),
        )
        self.assertEqual(8, risk["max_possible_before_deadline"])
        self.assertTrue(risk["must_max_every_week"])
        self.assertFalse(risk["completion_impossible"])

        impossible = YgdkAdapter._weekly_risk(
            current=7, target=16, week_count=0, week_target=4,
            due_at="2030-12-22T23:59:59+08:00",
            now=datetime(2030, 12, 9, 9, tzinfo=china),
        )
        self.assertTrue(impossible["must_max_every_week"])
        self.assertTrue(impossible["completion_impossible"])

    def test_libbook_cas_and_booking_mapping(self):
        self.assertEqual(
            "ticket+value",
            LibBookAdapter.extract_cas("https://booking.lib.buaa.edu.cn/v4/login/cas?cas=ticket%2Bvalue"),
        )
        self.assertEqual(
            "fragment+value",
            LibBookAdapter.extract_cas(
                "https://booking.lib.buaa.edu.cn/h5/index.html#/cas?cas=fragment%2Bvalue"
            ),
        )
        adapter = LibBookAdapter(object())
        event = adapter._event({
            "id": "B1", "name": "二层阅览区", "no": "A-12", "day": "2030-09-19",
            "beginTime": "08:00", "endTime": "10:00", "status": "1", "status_name": "待使用",
        })
        self.assertEqual("booking", event["kind"])
        self.assertEqual("upcoming", event["status"])
        self.assertTrue(event["starts_at"].startswith("2030-09-19T08:00:00"))
        self.assertEqual("二层阅览区", event["location"])


if __name__ == "__main__":
    unittest.main()
