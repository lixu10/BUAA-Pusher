import unittest
from unittest.mock import AsyncMock

from app.adapters.byxt import CURRENT_USER, PORTAL_VERIFY, SSO_LOGIN, ByxtAdapter, CourseReminderAdapter, _parse_date, _teacher
from app.upstream.sso import AuthenticationRequired


class FakeResponse:
    def __init__(self, url: str, status_code: int = 200, text: str = "{}"):
        self.url = url
        self.status_code = status_code
        self.text = text


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class FakeSession:
    authenticated = True

    def __init__(self, responses):
        self.client = FakeClient(responses)
        self.user = {"id": "test"}


class ByxtLoginTest(unittest.IsolatedAsyncioTestCase):
    def test_school_week_datetime_and_teacher_are_normalized(self):
        self.assertEqual("2030-09-16", _parse_date("2030-09-16 00:00:00").isoformat())
        self.assertEqual("王老师", _teacher({"weeksAndTeachers": "1-16周[理论]/王老师[主讲]"}))

    async def test_exchanges_cas_service_before_probe(self):
        session = FakeSession([
            FakeResponse(PORTAL_VERIFY),
            FakeResponse(CURRENT_USER, text='{"code":"0"}'),
        ])
        await ByxtAdapter(session)._ensure_login()
        self.assertEqual(SSO_LOGIN, session.client.calls[0][0])
        self.assertEqual({"service": PORTAL_VERIFY}, session.client.calls[0][1]["params"])
        self.assertEqual(CURRENT_USER, session.client.calls[1][0])

    async def test_rejects_unauthorized_probe(self):
        session = FakeSession([
            FakeResponse(PORTAL_VERIFY),
            FakeResponse(CURRENT_USER, status_code=401, text=""),
        ])
        with self.assertRaises(AuthenticationRequired):
            await ByxtAdapter(session)._ensure_login()

    def test_manifest_records_reviewed_contract(self):
        manifest = ByxtAdapter.manifest.public()
        self.assertIn("exams", manifest["capabilities"])
        self.assertIn(CURRENT_USER, manifest["endpoints"])
        self.assertTrue(any("buaa-api" in item for item in manifest["references"]))

    async def test_exam_route_failure_keeps_course_result(self):
        adapter = ByxtAdapter(FakeSession([]))
        adapter._ensure_login = AsyncMock()

        async def response_for(_method, path, **_kwargs):
            if path.endswith("schoolCalendars.do"):
                return {"code": "0", "datas": [{"selected": True, "itemCode": "2030-1"}]}
            if path.endswith("getTermWeeks.do"):
                return {"code": "0", "datas": [{
                    "curWeek": True, "serialNumber": 1, "startDate": "2030-09-16",
                }]}
            if path.endswith("getMyScheduleDetail.do"):
                return {"code": "0", "datas": {"arrangedList": [{
                    "courseCode": "C1", "courseName": "测试课程", "dayOfWeek": 1,
                    "beginSection": 1, "beginTime": "08:00", "endTime": "09:35",
                }]}}
            raise RuntimeError("exam route changed")

        adapter._json = AsyncMock(side_effect=response_for)
        events = await adapter.sync()
        self.assertEqual(["course"], [event["kind"] for event in events])
        self.assertEqual(1, adapter.last_error_count)
        self.assertIn("考试", adapter.last_error_detail)

    def test_course_identity_contains_week_date(self):
        adapter = ByxtAdapter(FakeSession([]))
        row = {
            "courseCode": "C1", "courseSerialNo": "01", "courseName": "测试课程",
            "dayOfWeek": 1, "beginSection": 1, "beginTime": "08:00", "endTime": "09:35",
        }
        first = adapter._courses([row], {"startDate": "2030-09-02 00:00:00"})[0]
        second = adapter._courses([row], {"startDate": "2030-09-09 00:00:00"})[0]
        self.assertNotEqual(first["external_id"], second["external_id"])

    async def test_course_reminder_only_reads_byxt_schedule_and_not_exams_or_signin(self):
        adapter = CourseReminderAdapter(FakeSession([]))
        adapter._ensure_login = AsyncMock()
        async def response_for(_method, path, **_kwargs):
            if path.endswith("schoolCalendars.do"):
                return {"datas": [{"selected": True, "itemCode": "2030-1"}]}
            if path.endswith("getTermWeeks.do"):
                return {"datas": [{"serialNumber": 1, "startDate": "2030-09-16"}]}
            if path.endswith("getMyScheduleDetail.do"):
                return {"datas": {"arrangedList": [{"courseCode": "C1", "courseName": "课程",
                    "dayOfWeek": 1, "beginTime": "08:00", "endTime": "09:35",
                    "placeName": "教室", "weeksAndTeachers": "1-16周[理论]/王老师[主讲]"}]}}
            raise AssertionError(f"Unexpected endpoint: {path}")
        adapter._json = AsyncMock(side_effect=response_for)
        events = await adapter.sync()
        self.assertEqual(3, adapter._json.await_count)
        self.assertEqual(["course"], [e["kind"] for e in events])
        self.assertEqual("教室", events[0]["location"])
        self.assertEqual("王老师", events[0]["metadata"]["teacher"])
        self.assertNotIn("exams", adapter.manifest.capabilities)
        self.assertTrue(all("iclass" not in url and "sign" not in url and "exams.do" not in url
                            for url in adapter.manifest.endpoints))


if __name__ == "__main__":
    unittest.main()
