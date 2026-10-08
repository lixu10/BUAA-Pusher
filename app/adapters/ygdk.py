from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from app.upstream.sso import AuthenticationRequired, BuaaSsoSession

from .base import ConnectorManifest, SourceAdapter
from .byxt import ByxtAdapter


FRONT_BASE = "https://ygdk.buaa.edu.cn"
API_BASE = f"{FRONT_BASE}/api"
APP_ID = "200230221144501510"
OAUTH_URL = (
    "https://app.buaa.edu.cn/uc/api/oauth/index"
    f"?redirect={quote(f'{FRONT_BASE}/#/home', safe='')}&appid={APP_ID}&state=STATE&qrcode=1"
)
CLASSIFY_URL = f"{API_BASE}/Front/Clockin/Classify/getList"
COUNT_URL = f"{API_BASE}/Front/Clockin/Clockin/getCount"
TERM_URL = f"{API_BASE}/Front/Clockin/Term/get"
LOGIN_URL = f"{API_BASE}/Front/Clockin/User/campusAppLogin"


class YgdkAdapter(SourceAdapter):
    id = "ygdk"
    label = "阳光体育"
    manifest = ConnectorManifest(
        id=id,
        label=label,
        version="2026-09-19",
        capabilities=("fitness_progress", "term_target", "progress_changes"),
        endpoints=(OAUTH_URL, LOGIN_URL, CLASSIFY_URL, COUNT_URL, TERM_URL),
        references=(
            "BUAASubnet/UBAA:YgdkClient.kt",
            "BUAASubnet/UBAA:YgdkService.kt",
        ),
    )

    def __init__(self, session: BuaaSsoSession):
        self.session = session
        self.uid = ""
        self.token = ""

    async def sync(self) -> list[dict[str, Any]]:
        if not self.session.authenticated:
            raise AuthenticationRequired("需要登录北航统一认证")
        await self._login()
        classifies = await self._post(CLASSIFY_URL)
        rows = classifies.get("list") or []
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("阳光体育未返回活动分类")
        classify = self.choose_classify(rows)
        classify_id = self._int(classify.get("classify_id"))
        if classify_id is None:
            raise RuntimeError("阳光体育分类缺少 classify_id")

        count = await self._post(COUNT_URL, {
            "classify_id": str(classify_id), "user_id": self.uid,
        })
        term = await self._post(TERM_URL)
        current = self.first_int(
            count, "term_good_count_show", "term_good_count", "term_count_show", "term_count"
        ) or 0
        target = self.first_int(count, "term_num")
        if target is None:
            target = self._int(classify.get("term_num"))
        term_start, due_at = self._term_bounds(term)
        if due_at is None:
            due_at = await self._term_end()
        term_id = self.first_int(term, "term_id", "id")
        return [self._event(
            classify, count, term, current, target, term_id, term_start, due_at
        )]

    async def _login(self) -> None:
        current = OAUTH_URL
        code: str | None = None
        for _ in range(10):
            code = self.extract_code(current)
            if code:
                break
            response = await self.session.client.get(current, follow_redirects=False)
            code = self.extract_code(str(response.url))
            location = response.headers.get("location")
            if not code and location:
                current = urljoin(str(response.url), location)
                code = self.extract_code(current)
            if code:
                break
            if not location:
                if response.status_code == 401 or "sso.buaa.edu.cn" in str(response.url):
                    raise AuthenticationRequired("阳光体育授权未完成")
                break
        if not code:
            raise AuthenticationRequired("未能从阳光体育登录链获取 code")

        response = await self.session.client.get(LOGIN_URL, params={"code": code})
        result = self._unwrap(response, "阳光体育登录")
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        uid, token = self._int(data.get("uid")), str(data.get("token") or "").strip()
        if uid is None or not token:
            raise AuthenticationRequired("阳光体育登录响应缺少 uid 或 token")
        self.uid, self.token = str(uid), unquote(token)

    async def _post(self, url: str, values: dict[str, str] | None = None) -> dict[str, Any]:
        data = dict(values or {})
        data.update({"uid": self.uid, "token": self.token})
        response = await self.session.client.post(
            url,
            data=data,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        return self._unwrap(response, "阳光体育接口")

    @staticmethod
    def _unwrap(response: Any, label: str) -> dict[str, Any]:
        if response.status_code in {401, 403} or "sso.buaa.edu.cn" in str(response.url):
            raise AuthenticationRequired(f"{label}授权已失效")
        if response.status_code != 200:
            raise RuntimeError(f"{label}返回 {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{label}返回了非 JSON 内容") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{label}字段结构已变化")
        code = YgdkAdapter._int(payload.get("code"))
        if code == -98:
            raise AuthenticationRequired("阳光体育会话已失效")
        if code != 1:
            raise RuntimeError(str(payload.get("msg") or f"{label}业务错误"))
        result = payload.get("result")
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise RuntimeError(f"{label}结果字段结构已变化")
        return result

    async def _term_end(self) -> str | None:
        """Use the selected BYXT term's final week as a best-effort deadline."""
        try:
            byxt = ByxtAdapter(self.session)
            await byxt._ensure_login()
            terms = (await byxt._json("GET", "api/home/student/schoolCalendars.do")).get("datas", [])
            selected = next((row for row in terms if row.get("selected")), terms[0] if terms else None)
            term_code = str((selected or {}).get("itemCode") or "")
            if not term_code:
                return None
            weeks = (await byxt._json(
                "GET", "api/home/getTermWeeks.do", params={"termCode": term_code}
            )).get("datas", [])
            dates = [str(row.get("endDate") or "") for row in weeks if isinstance(row, dict)]
            end = max((value for value in dates if value), default="")
            if not end:
                return None
            parsed = datetime.fromisoformat(end.replace(" ", "T"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
            if parsed.time() == time.min:
                parsed = parsed.replace(hour=23, minute=59, second=59)
            return parsed.isoformat()
        except Exception:
            return None

    def _event(
        self,
        classify: dict[str, Any],
        count: dict[str, Any],
        term: dict[str, Any],
        current: int,
        target: int | None,
        term_id: int | None,
        term_start: str | None,
        due_at: str | None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        classify_id = self._int(classify.get("classify_id")) or 0
        classify_name = str(classify.get("name") or "阳光体育")
        term_name = str(term.get("name") or "当前学期")
        completed = target is not None and target > 0 and current >= target
        week_count = self._int(count.get("week_count")) or 0
        week_target = self.first_int(count, "week_num") or self._int(classify.get("week_num"))
        risk = self._weekly_risk(
            current=current,
            target=target,
            week_count=week_count,
            week_target=week_target,
            due_at=due_at,
            now=now,
        )
        schedule_anchor = self._term_week_anchor(term_start)
        return {
            "source": self.id,
            "external_id": f"fitness-{term_id or 'current'}-{classify_id}",
            "kind": "fitness",
            "title": f"{classify_name} · {current}/{target if target is not None else '-'}",
            # Weekly risk rules repeat from the Monday containing the term
            # start.  Progress changes do not move this stable schedule.
            "starts_at": schedule_anchor,
            "due_at": due_at,
            "status": "done" if completed else "pending",
            "metadata": {
                "classify_id": classify_id,
                "classify_name": classify_name,
                "term_id": term_id,
                "term_name": term_name,
                "current": current,
                "target": target,
                "remaining": max(target - current, 0) if target is not None else None,
                "term_start": term_start,
                "term_end": due_at,
                "week_count": week_count,
                "week_target": week_target,
                "month_count": self._int(count.get("month_count")),
                "month_target": self.first_int(count, "month_num")
                or self._int(classify.get("month_num")),
                **risk,
            },
        }

    @staticmethod
    def _term_bounds(term: dict[str, Any]) -> tuple[str | None, str | None]:
        return (
            YgdkAdapter._timestamp(term.get("start_time")),
            YgdkAdapter._timestamp(term.get("end_time")),
        )

    @staticmethod
    def _timestamp(value: Any) -> str | None:
        try:
            timestamp = int(value)
        except (TypeError, ValueError):
            return None
        china = timezone(timedelta(hours=8))
        return datetime.fromtimestamp(timestamp, UTC).astimezone(china).isoformat()

    @staticmethod
    def _term_week_anchor(term_start: str | None) -> str | None:
        if not term_start:
            return None
        try:
            parsed = datetime.fromisoformat(term_start.replace("Z", "+00:00"))
        except ValueError:
            return None
        monday = (parsed - timedelta(days=parsed.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return monday.isoformat()

    @staticmethod
    def _weekly_risk(
        *, current: int, target: int | None, week_count: int,
        week_target: int | None, due_at: str | None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "weeks_remaining": None,
            "current_week_capacity": None,
            "max_possible_before_deadline": None,
            "must_max_every_week": False,
            "completion_impossible": False,
        }
        if target is None or not week_target or not due_at:
            return result
        try:
            deadline = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        except ValueError:
            return result
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone(timedelta(hours=8)))
        local_now = now or datetime.now(deadline.tzinfo)
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=deadline.tzinfo)
        else:
            local_now = local_now.astimezone(deadline.tzinfo)
        remaining = max(target - current, 0)
        if remaining == 0 or deadline < local_now:
            result["completion_impossible"] = remaining > 0
            result["max_possible_before_deadline"] = 0
            result["weeks_remaining"] = 0
            return result
        days_to_sunday = 6 - local_now.weekday()
        next_monday = local_now.date() + timedelta(days=days_to_sunday + 1)
        future_days = max((deadline.date() - next_monday).days + 1, 0)
        future_weeks = (future_days + 6) // 7
        current_capacity = max(week_target - week_count, 0)
        maximum = current_capacity + future_weeks * week_target
        result.update({
            "weeks_remaining": 1 + future_weeks,
            "current_week_capacity": current_capacity,
            "max_possible_before_deadline": maximum,
            # Equality means every remaining weekly allowance is required;
            # greater than means the target is already mathematically impossible.
            "must_max_every_week": remaining >= maximum,
            "completion_impossible": remaining > maximum,
        })
        return result

    @staticmethod
    def extract_code(url: str) -> str | None:
        parsed = urlparse(url)
        queries = [parsed.query]
        if "?" in parsed.fragment:
            queries.append(parsed.fragment.split("?", 1)[1])
        for query in queries:
            values = parse_qs(query)
            code = (values.get("code") or [None])[0]
            if code:
                return code
        return None

    @staticmethod
    def choose_classify(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return (
            next((row for row in rows if "体育" in str(row.get("name") or "")), None)
            or next((row for row in rows if YgdkAdapter._int(row.get("classify_id")) == 1), None)
            or rows[0]
        )

    @staticmethod
    def first_int(source: dict[str, Any], *keys: str) -> int | None:
        for key in keys:
            value = YgdkAdapter._int(source.get(key))
            if value is not None:
                return value
        return None

    @staticmethod
    def _int(value: Any) -> int | None:
        try:
            return int(value) if value is not None and str(value).strip() else None
        except (TypeError, ValueError):
            return None
