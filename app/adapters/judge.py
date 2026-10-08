from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.upstream.sso import AuthenticationRequired, BuaaSsoSession
from app.services.academic import in_academic_window

from .base import ConnectorManifest, SourceAdapter


BASE = "https://judge.buaa.edu.cn"
SERVICE_LOGIN = "https://sso.buaa.edu.cn/login?service=http%3A%2F%2Fjudge.buaa.edu.cn%2F"
COURSES_URL = f"{BASE}/courselist.jsp?courseID=0"
ASSIGNMENTS_URL = f"{BASE}/assignment/index.jsp"


class _AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current_href: str | None = None
        self.current_text: list[str] = []
        self.anchors: list[tuple[str, str]] = []
        self.text: list[str] = []
        self.rows: list[list[str]] = []
        self.table_depth = 0
        self.cells: list[str] | None = None
        self.cell_text: list[str] | None = None
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self.ignored += 1
        if tag == "table":
            self.table_depth += 1
        if tag == "tr" and self.table_depth == 1:
            self.cells = []
        if tag in {"td", "th"} and self.table_depth == 1 and self.cells is not None:
            self.cell_text = []
        if tag.lower() == "a":
            self.current_href = dict(attrs).get("href")
            self.current_text = []

    def handle_data(self, data: str) -> None:
        if self.ignored:
            return
        self.text.append(data)
        if self.cell_text is not None:
            self.cell_text.append(data)
        if self.current_href is not None:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self.ignored = max(0, self.ignored - 1)
        if tag in {"td", "th"} and self.table_depth == 1 and self.cell_text is not None:
            self.cells.append(re.sub(r"\s+", " ", " ".join(self.cell_text)).strip())
            self.cell_text = None
        if tag == "tr" and self.table_depth == 1 and self.cells is not None:
            self.rows.append(self.cells)
            self.cells = None
        if tag == "table":
            self.table_depth = max(0, self.table_depth - 1)
        if tag.lower() == "a" and self.current_href is not None:
            self.anchors.append((self.current_href, " ".join(self.current_text)))
            self.current_href = None
            self.current_text = []

    @property
    def plain_text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.text)).strip()


class JudgeAdapter(SourceAdapter):
    id = "judge"
    label = "JUDGE 作业"
    manifest = ConnectorManifest(
        id=id,
        label=label,
        version="2026-09-19",
        capabilities=("assignments", "submission_status", "scores"),
        endpoints=(SERVICE_LOGIN, COURSES_URL, ASSIGNMENTS_URL),
        references=(
            "BUAASubnet/UBAA:JudgeClient.kt",
            "BUAASubnet/UBAA:JudgeSupport.kt",
        ),
    )

    def __init__(self, session: BuaaSsoSession, previous_events: list[dict[str, Any]] | None = None,
                 academic_window: dict[str, Any] | None = None):
        self.session = session
        self.previous = {e["external_id"]: e for e in (previous_events or []) if e["source"] == self.id}
        self.academic_window = academic_window
        self.last_error_count = 0
        self.last_error_detail = ""
        self.headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

    async def sync(self) -> list[dict[str, Any]]:
        if not self.session.authenticated:
            raise AuthenticationRequired("需要登录北航统一认证")
        activation = await self.session.client.get(SERVICE_LOGIN, headers=self.headers)
        self._ensure_html(activation, "JUDGE 授权")
        courses = self.parse_links(await self._get(COURSES_URL), "courseID", exclude="0")
        events: list[dict[str, Any]] = []
        completed = total = 0
        for course_id, course_name in courses:
            await self._get(f"{BASE}/courselist.jsp?courseID={course_id}")
            assignments = self.parse_links(await self._get(ASSIGNMENTS_URL), "assignID")
            total += len(assignments)
            for assignment_id, title in assignments:
                previous = self.previous.get(f"assignment-{course_id}-{assignment_id}")
                self.report_progress(completed, total, "读取作业与提交进度")
                if previous and self.academic_window and not in_academic_window(previous, self.academic_window):
                    events.append(previous)
                else:
                    try:
                        detail = await self._get(f"{ASSIGNMENTS_URL}?assignID={assignment_id}")
                        events.append(self._event(course_id, course_name, assignment_id, title, detail))
                    except AuthenticationRequired:
                        raise
                    except Exception:
                        self.last_error_count += 1
                        self.last_error_detail = f"{self.last_error_count} 项作业详情不可用，保留旧数据"
                        if previous:
                            events.append(previous)
                completed += 1
                self.report_progress(completed, total, "读取作业与提交进度")
        return events

    async def _get(self, url: str) -> str:
        response = await self.session.client.get(url, headers=self.headers)
        self._ensure_html(response, "JUDGE")
        return response.text

    @staticmethod
    def _ensure_html(response: Any, label: str) -> None:
        body, url = response.text, str(response.url)
        if response.status_code == 401 or "sso.buaa.edu.cn/login" in url:
            raise AuthenticationRequired(f"{label}登录状态已失效")
        if "input name=\"execution\"" in body or "统一身份认证" in body:
            raise AuthenticationRequired(f"{label}登录状态已失效")
        if response.status_code != 200:
            raise RuntimeError(f"{label}返回 {response.status_code}")

    @staticmethod
    def parse_links(html: str, query_key: str, exclude: str | None = None) -> list[tuple[str, str]]:
        parser = _AnchorParser()
        parser.feed(html)
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        for href, raw_text in parser.anchors:
            if query_key == "assignID" and any(path in href for path in ("problemContent", "judgeDetails")):
                continue
            values = parse_qs(urlparse(href).query).get(query_key) or []
            if not values:
                match = re.search(rf"{re.escape(query_key)}=(\d+)", href)
                values = [match.group(1)] if match else []
            if not values or values[0] == exclude or values[0] in seen:
                continue
            text = re.sub(r"\s+", " ", raw_text).strip()
            if text:
                seen.add(values[0])
                result.append((values[0], text))
        return result

    def _event(self, course_id: str, course_name: str, assignment_id: str, title: str, html: str) -> dict[str, Any]:
        parser = _AnchorParser()
        parser.feed(html)
        text = parser.plain_text
        times = re.search(
            r"作业时间[：:]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)\s*至\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)",
            text,
        )
        problem_states = []
        for cells in parser.rows:
            if len(cells) >= 4 and re.fullmatch(r"\d+(?:\.\d+)?", cells[2]):
                status_text = " ".join(cells[3:])
            elif len(cells) == 2 and re.fullmatch(r"\d+\.?", cells[0]):
                status_text = cells[1]
            else:
                continue
            detected = self._submission_evidence(status_text)
            if detected:
                problem_states.append(detected)
        declared_total = int(self._match(text, r"共\s*(\d+)\s*道") or 0)
        total = declared_total or len(problem_states)
        submitted_count = sum(state == "submitted" for state in problem_states)
        if total:
            if not problem_states:
                submitted_count = len(re.findall(r"(?:初次|首次|最近一次|最后一次)提交时间", text))
            state = "submitted" if submitted_count >= total else "partial" if submitted_count else (
                "unsubmitted" if self._submission_evidence(text) == "unsubmitted" else "unknown")
        else:
            state = self._submission_evidence(text) or "unknown"
        type_text = self._match(text, r"作业类型[：:]\s*([^\s]+)")
        max_score = self._match(text, r"作业满分[：:]\s*([\d.]+)")
        my_score = self._match(text, r"总分[：:]\s*([\d.]+)")
        opens_at = self._date_time(times.group(1)) if times else None
        due_at = self._date_time(times.group(2)) if times else None
        return {
            "source": self.id,
            "external_id": f"assignment-{course_id}-{assignment_id}",
            "kind": "assignment",
            "title": f"{course_name} · {title}",
            # An assignment belongs on the calendar at its deadline.  Keep the
            # availability window as detail instead of making it the primary
            # calendar time (most JUDGE assignments open on the same day).
            "starts_at": None,
            "due_at": due_at,
            "status": {"submitted": "done", "unsubmitted": "pending", "partial": "partial"}.get(state, "unknown"),
            "metadata": {
                "course_id": course_id, "assignment_id": assignment_id,
                "course_name": course_name, "submitted": state == "submitted",
                "opens_at": opens_at, "max_score": max_score, "score": my_score,
                "submission_status": state, "submitted_count": submitted_count,
                "total_problems": total, "assignment_type": type_text or "",
                "status_source": "assignment_detail",
            },
        }

    @staticmethod
    def _submission_evidence(text: str) -> str | None:
        if any(marker in text for marker in ("还未提交代码", "未提交文件", "未提交答案", "未作答", "未提交")):
            return "unsubmitted"
        if any(marker.lower() in text.lower() for marker in (
            "初次提交时间", "首次提交时间", "最近一次提交时间", "最后一次提交时间",
            "最后一次修改时间", "已提交", "得分", "Accepted", "Accept")):
            return "submitted"
        return None

    @staticmethod
    def _match(text: str, pattern: str) -> str | None:
        match = re.search(pattern, text)
        return match.group(1) if match else None

    @staticmethod
    def _date_time(value: str) -> str | None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=timezone(timedelta(hours=8))).isoformat()
            except ValueError:
                continue
        return None
