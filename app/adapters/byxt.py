from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from app.upstream.sso import AuthenticationRequired, BuaaSsoSession

from .base import ConnectorManifest, SourceAdapter


BASE = "https://byxt.buaa.edu.cn/jwapp/sys/homeapp"
CURRENT_USER = f"{BASE}/api/home/currentUser.do"
PORTAL_VERIFY = f"{BASE}/index.do?contextPath=/jwapp"
SSO_LOGIN = "https://sso.buaa.edu.cn/login"


class ByxtAdapter(SourceAdapter):
    include_exams = True
    id = "byxt"
    label = "本科教务"
    manifest = ConnectorManifest(
        id=id,
        label=label,
        version="2026-09-19",
        capabilities=("terms", "weeks", "courses", "exams"),
        endpoints=(
            CURRENT_USER,
            f"{BASE}/api/home/student/schoolCalendars.do",
            f"{BASE}/api/home/getTermWeeks.do",
            f"{BASE}/api/home/student/getMyScheduleDetail.do",
            f"{BASE}/api/home/student/exams.do",
        ),
        references=(
            "BUAASubnet/UBAA:ScheduleService.kt",
            "BUAASubnet/UBAA:ExamService.kt",
            "BUAASubnet/UBAA:ByxtService.kt",
            "fontlos/buaa-api:src/api/aas/core.rs",
        ),
    )

    def __init__(self, session: BuaaSsoSession):
        self.session = session
        self.last_errors: list[str] = []

    @property
    def last_error_count(self) -> int:
        return len(self.last_errors)

    @property
    def last_error_detail(self) -> str:
        return "；".join(self.last_errors)

    async def _ensure_login(self) -> None:
        if not self.session.authenticated:
            raise AuthenticationRequired("需要登录北航统一认证")
        # BYXT has its own CAS service session. Exchange the existing SSO TGC for
        # a BYXT ticket first (buaa-api), then probe currentUser.do (UBAA).
        exchange = await self.session.client.get(SSO_LOGIN, params={"service": PORTAL_VERIFY})
        if not str(exchange.url).startswith(PORTAL_VERIFY):
            raise AuthenticationRequired("本科教务 CAS 授权失败")
        response = await self.session.client.get(CURRENT_USER)
        if response.status_code == 401 or "sso.buaa.edu.cn" in str(response.url):
            raise AuthenticationRequired("本科教务授权未完成")
        if response.status_code != 200:
            raise RuntimeError(f"本科教务入口返回 {response.status_code}")
        body = response.text.lstrip()
        if not (body.startswith("{") or body.startswith("[")):
            raise RuntimeError("本科教务入口响应格式已变化")

    async def _json(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE}/index.html",
        }
        response = await self.session.client.request(method, f"{BASE}/{path.lstrip('/')}", headers=headers, **kwargs)
        if response.status_code == 401 or "sso.buaa.edu.cn" in str(response.url) or "name=\"execution\"" in response.text:
            self.session.user = None
            raise AuthenticationRequired("教务会话已过期")
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("教务接口返回了非 JSON 内容") from exc
        if str(payload.get("code")) != "0":
            raise RuntimeError(payload.get("msg") or "教务接口业务错误")
        return payload

    async def sync(self) -> list[dict[str, Any]]:
        await self._ensure_login()
        self.last_errors = []
        terms = (await self._json("GET", "api/home/student/schoolCalendars.do")).get("datas", [])
        term = next((item for item in terms if item.get("selected")), terms[0] if terms else None)
        if not term:
            raise RuntimeError("教务未返回当前学期，已保留上次课表")
        term_code = str(term["itemCode"])
        events: list[dict[str, Any]] = []
        try:
            weeks = (await self._json("GET", "api/home/getTermWeeks.do", params={"termCode": term_code})).get("datas", [])
            if not isinstance(weeks, list) or not weeks:
                raise RuntimeError("教务未返回教学周，无法确认课表完整性")
            # A calendar must contain the term, not only the week in which a
            # sync happened.  BYXT returns about 18-20 records here, so the
            # initial import is still small enough to fetch in one pass.
            for index, week in enumerate(weeks):
                self.report_progress(index, len(weeks), "读取课程表")
                weekly = await self._json(
                    "POST", "api/home/student/getMyScheduleDetail.do",
                    data={"termCode": term_code, "type": "week", "week": str(week.get("serialNumber"))},
                )
                arranged = (weekly.get("datas") or {}).get("arrangedList")
                if not isinstance(arranged, list):
                    raise RuntimeError("教务课表结构变化，不能判定课程取消")
                events.extend(self._courses(arranged, {**week, "term_code": term_code}))
            self.report_progress(len(weeks), len(weeks), "读取课程表")
        except AuthenticationRequired:
            raise
        except Exception as exc:
            self.last_errors.append(f"课表：{str(exc)[:80]}")
        if self.include_exams:
            try:
                exams = await self._json("GET", "api/home/student/exams.do", params={"termCode": term_code})
                events.extend(self._exams(exams.get("datas", []), term_code))
            except AuthenticationRequired:
                raise
            except Exception as exc:
                self.last_errors.append(f"考试：{str(exc)[:80]}")
        return events

    async def close(self) -> None:
        await self.session.close()

    def _courses(self, courses: list[dict[str, Any]], week: dict[str, Any]) -> list[dict[str, Any]]:
        start = _parse_date(week.get("startDate"))
        if not start:
            raise RuntimeError(f"无法识别教学周日期：{week.get('startDate')}")
        result = []
        for item in courses:
            day = int(item.get("dayOfWeek") or 1)
            course_date = start + timedelta(days=max(0, day - 1))
            begins = _combine(course_date, item.get("beginTime"))
            ends = _combine(course_date, item.get("endTime"))
            # Include the actual date.  The same course/section repeats every
            # week and previously overwrote itself in SQLite.
            key = "|".join(
                str(value or "") for value in (
                    item.get("courseCode"), item.get("courseSerialNo"),
                    course_date.isoformat(), item.get("dayOfWeek"), item.get("beginSection"),
                )
            )
            result.append({
                "source": self.id,
                "external_id": f"course-{hashlib.sha1(key.encode()).hexdigest()[:14]}",
                "kind": "course",
                "title": item.get("courseName") or "课程",
                "starts_at": begins,
                "ends_at": ends,
                "location": item.get("placeName"),
                "status": "upcoming",
                "metadata": {**item, "teacher": _teacher(item), "term_code": week.get("term_code"),
                             "week_start": start.isoformat(), "week_number": week.get("serialNumber")},
            })
        return result

    def _exams(self, exams: list[dict[str, Any]], term_code: str) -> list[dict[str, Any]]:
        result = []
        for item in exams:
            exam_date = _parse_date(item.get("examDate"))
            begins = _combine(exam_date, item.get("startTime")) if exam_date else None
            ends = _combine(exam_date, item.get("endTime")) if exam_date else None
            key = str(item.get("taskId") or f"{term_code}-{item.get('courseNo')}-{item.get('examDate')}")
            result.append({
                "source": self.id,
                "external_id": f"exam-{key}",
                "kind": "exam",
                "title": item.get("courseName") or "考试",
                "starts_at": begins,
                "ends_at": ends,
                "location": item.get("examPlace"),
                "status": "upcoming",
                "metadata": item,
            })
        return result


class CourseReminderAdapter(ByxtAdapter):
    """Course-only BYXT reader. No iClass session or attendance endpoint."""

    label = "课程提醒"
    include_exams = False
    cleanup_kinds = ("course",)
    manifest = ConnectorManifest(
        id="byxt", label=label, version="2026-10-08",
        capabilities=("terms", "weeks", "courses"),
        endpoints=tuple(endpoint for endpoint in ByxtAdapter.manifest.endpoints
                        if not endpoint.endswith("exams.do")),
        references=("BUAASubnet/UBAA:ScheduleService.kt", "fontlos/buaa-api:src/api/aas/core.rs"),
    )


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _combine(day: date, value: Any) -> str | None:
    if not value:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(str(value), fmt).time()
            china = timezone(timedelta(hours=8))
            return datetime.combine(day, time(parsed.hour, parsed.minute, parsed.second), china).isoformat()
        except ValueError:
            continue
    return None


def _teacher(item: dict[str, Any]) -> str | None:
    raw = str(item.get("weeksAndTeachers") or "")
    names = [
        value.strip() for value in re.findall(r"(?:^|/)([^/\[]+)\[[^\]]+\]", raw)
        if value.strip() and "周" not in value and not any(char.isdigit() for char in value)
    ]
    if names:
        return "、".join(dict.fromkeys(names))
    for value in item.get("titleDetail") or []:
        text = str(value)
        if text.startswith("上课教师："):
            return text.split("：", 1)[1].split("/", 1)[0].strip() or None
    return None
