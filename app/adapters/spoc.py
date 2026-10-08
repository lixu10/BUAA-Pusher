from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.upstream.sso import AuthenticationRequired, BuaaSsoSession

from .base import ConnectorManifest, SourceAdapter


BASE = "https://spoc.buaa.edu.cn/spocnewht"
CAS_URL = f"{BASE}/cas"
CAS_LOGIN_URL = f"{BASE}/sys/casLogin"
CURRENT_TERM_URL = f"{BASE}/inco/ht/queryOne"
COURSES_URL = f"{BASE}/jxkj/queryKclb"
ASSIGNMENTS_URL = f"{BASE}/inco/ht/queryListByPage"
DETAIL_URL = f"{BASE}/kczy/queryKczyInfoByid"
SUBMISSION_URL = f"{BASE}/kczy/queryXsSubmitKczyInfo"
CURRENT_TERM_PARAM = "YHrxtTavu6raCwC0/qdgYffB9evWHBkTng/XS4W6j3f/TPo02iEPSoegscDTRNzIPRG49o3RHl4JiFCXAiBkkA=="
ASSIGNMENTS_SQL_ID = "1713252980496efac7d5d9985e81693116d3e8a52ebf2b"


class SpocAdapter(SourceAdapter):
    id = "spoc"
    label = "SPOC 作业"
    manifest = ConnectorManifest(
        id=id,
        label=label,
        version="2026-09-19",
        capabilities=("assignments", "submission_status", "grading"),
        endpoints=(CAS_URL, CAS_LOGIN_URL, CURRENT_TERM_URL, COURSES_URL, ASSIGNMENTS_URL, DETAIL_URL, SUBMISSION_URL),
        references=(
            "BUAASubnet/UBAA:SpocClient.kt",
            "BUAASubnet/UBAA:SpocSupport.kt",
            "fontlos/buaa-api:src/api/spoc",
        ),
    )

    def __init__(self, session: BuaaSsoSession):
        self.session = session
        self.token = ""
        self.role_code = ""

    async def sync(self) -> list[dict[str, Any]]:
        if not self.session.authenticated:
            raise AuthenticationRequired("需要登录北航统一认证")
        for attempt in range(2):
            try:
                await self._login()
                return await self._sync_authenticated()
            except AuthenticationRequired:
                self.token = ""
                self.role_code = ""
                if attempt:
                    raise
        return []

    async def _sync_authenticated(self) -> list[dict[str, Any]]:
        term = await self._post(CURRENT_TERM_URL, {"param": CURRENT_TERM_PARAM})
        # mrxq is the machine term code (for example 2026-20271); dqxq is only
        # the human-readable label.  Querying with dqxq succeeds but returns 0.
        term_code = str(term.get("mrxq") or "")
        if not term_code:
            raise RuntimeError("SPOC 未返回当前学期")
        courses = await self._get(COURSES_URL, {"kcmc": "", "xnxq": term_code})
        course_map = {
            str(item.get("kcid") or ""): item
            for item in courses if isinstance(item, dict) and item.get("kcid") is not None
        }
        rows: list[dict[str, Any]] = []
        page_num = 1
        while page_num <= 20:
            plain = json.dumps({
                "pageSize": 50, "pageNum": page_num, "sqlid": ASSIGNMENTS_SQL_ID,
                "xnxq": term_code, "kcid": "", "yzwz": "",
            }, ensure_ascii=False, separators=(",", ":"))
            content = await self._post(ASSIGNMENTS_URL, {"param": self.encrypt_param(plain)})
            page_rows = content.get("list") or []
            if not isinstance(page_rows, list):
                raise RuntimeError("SPOC 作业字段结构已变化")
            rows.extend(item for item in page_rows if isinstance(item, dict))
            pages = int(content.get("pages") or 1)
            if not content.get("hasNextPage") or page_num >= pages or not page_rows:
                break
            if page_num == 20:
                raise RuntimeError("SPOC 分页未读取完毕，已保留旧数据")
            page_num += 1
        events = []
        self.last_error_count = 0
        self.last_error_detail = ""
        for index, item in enumerate(rows):
            self.report_progress(index, len(rows), "读取提交状态")
            assignment_id = str(item.get("zyid") or "")
            event = self._event(term_code, item, course_map)
            if assignment_id:
                try:
                    detail = await self._get_object(DETAIL_URL, {"id": assignment_id})
                    submission = await self._get_object(SUBMISSION_URL, {"kczyid": assignment_id})
                    if detail:
                        event["due_at"] = self._date_time(detail.get("zyjzsj")) or event["due_at"]
                        event["metadata"]["opens_at"] = self._date_time(detail.get("zykssj")) or event["metadata"]["opens_at"]
                        event["metadata"]["max_score"] = detail.get("zyfs")
                        event["metadata"].pop("score", None)
                        event["metadata"]["assignment_type"] = str(detail.get("zylx") or "")
                    status = self.submission_status((submission or {}).get("tjzt"), bool(submission))
                    event["status"] = "done" if status == "submitted" else "pending" if status == "unsubmitted" else "unknown"
                    event["metadata"].update({"submission_status": status, "submitted": status == "submitted",
                        "submitted_at": self._date_time((submission or {}).get("tjsj")), "status_source": "submission_detail"})
                    event["metadata"].update(self.grading(detail or {}, submission))
                except AuthenticationRequired:
                    raise
                except Exception:
                    self.last_error_count += 1
                    self.last_error_detail = f"{self.last_error_count} 项提交详情不可用，保留列表状态"
                    event["metadata"]["status_source"] = "assignment_list"
                    event["metadata"]["grading_known"] = False
            events.append(event)
        self.report_progress(len(rows), len(rows), "读取提交状态")
        return events

    @classmethod
    def grading(cls, detail: dict[str, Any], submission: dict[str, Any] | None) -> dict[str, Any]:
        # Verified against SPOC's official student/read-only detail components:
        # zyfs is maximum points; pf/py are personal grade/comment; pyfs=2 is
        # teacher graded, pyfs=3 can be peer grading and must not trigger this rule.
        personal = submission or {}
        def field(name):
            value = personal.get(name)
            return detail.get(name) if value is None else value
        state = field("pyfs")
        teacher_graded = str(state) == "2"
        score = field("pf") if teacher_graded else None
        return {"grading_known": submission is None or state is not None
                or (field("pf") in (None, "") and not field("py")),
                "graded": teacher_graded, "grading_status": str(state) if state is not None else "unknown",
                "earned_score": score, "grade_comment": cls._plain_text(field("py")) if teacher_graded else None,
                "graded_at": cls._date_time(field("pysj")) if teacher_graded else None,
                "max_score": detail.get("zyfs")}

    @staticmethod
    def _plain_text(value: Any) -> str | None:
        if value is None:
            return None
        from html import unescape
        return unescape(re.sub(r"<[^>]+>", " ", str(value))).strip() or None

    async def _get_object(self, url: str, params: dict[str, str]) -> dict[str, Any] | None:
        response = await self.session.client.get(url, params=params, headers={
            "X-Requested-With": "XMLHttpRequest", "Token": f"Inco-{self.token}", "RoleCode": self.role_code})
        if response.status_code == 401 or "sso.buaa.edu.cn" in str(response.url):
            raise AuthenticationRequired("SPOC 会话已失效")
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("code")) != "200":
            message = str(payload.get("msg") or "SPOC 提交详情查询失败")
            if any(word in message.lower() for word in ("token", "登录", "权限")):
                raise AuthenticationRequired(message)
            raise RuntimeError(message)
        content = payload.get("content")
        if content is not None and not isinstance(content, dict):
            raise RuntimeError("SPOC 提交详情字段结构已变化")
        return content

    @staticmethod
    def submission_status(raw: Any, has_content: bool) -> str:
        value = "" if raw is None else str(raw).strip()
        if value in {"1", "已做", "已提交"}:
            return "submitted"
        if value in {"0", "未做", "未提交"} or not has_content:
            return "unsubmitted"
        return "unknown"

    async def _login(self) -> None:
        current = CAS_URL
        token = ""
        for _ in range(8):
            token = self.extract_token(current) or ""
            if token:
                break
            response = await self.session.client.get(current, follow_redirects=False)
            token = self.extract_token(str(response.url)) or ""
            location = response.headers.get("location")
            if not token and location:
                current = urljoin(str(response.url), location)
                token = self.extract_token(current) or ""
            if token:
                break
            if not location:
                raise AuthenticationRequired("SPOC 登录跳转缺少 Location")
        if not token:
            raise AuthenticationRequired("未能从 SPOC 登录链获取 token")
        self.token = token
        content = await self._post(CAS_LOGIN_URL, {"token": token}, role_required=False)
        role = content.get("jsdm") or content.get("rolecode") or content.get("jsdmList")
        self.role_code = self._first_string(role) or ""
        if not self.role_code:
            raise AuthenticationRequired("SPOC 登录成功但未获取到角色")

    async def _post(self, url: str, body: dict[str, Any], role_required: bool = True) -> dict[str, Any]:
        headers = {"X-Requested-With": "XMLHttpRequest", "Token": f"Inco-{self.token}"}
        if role_required:
            if not self.role_code:
                raise AuthenticationRequired("SPOC 角色未初始化")
            headers["RoleCode"] = self.role_code
        response = await self.session.client.post(url, json=body, headers=headers)
        if response.status_code == 401 or "sso.buaa.edu.cn" in str(response.url):
            raise AuthenticationRequired("SPOC 会话已失效")
        if response.status_code != 200:
            raise RuntimeError(f"SPOC 返回 {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("SPOC 返回了非 JSON 内容") from exc
        message = str(payload.get("msg") or payload.get("msg_en") or "SPOC 请求失败")
        if int(payload.get("code", -1)) != 200:
            if any(word in message.lower() for word in ("登录", "token", "未认证", "未登录", "权限")):
                raise AuthenticationRequired(message)
            raise RuntimeError(message)
        content = payload.get("content")
        if content is None:
            return {}
        if not isinstance(content, dict):
            raise RuntimeError("SPOC 响应字段结构已变化")
        return content

    async def _get(self, url: str, params: dict[str, str]) -> list[dict[str, Any]]:
        if not self.token or not self.role_code:
            raise AuthenticationRequired("SPOC 会话未初始化")
        response = await self.session.client.get(
            url,
            params=params,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Token": f"Inco-{self.token}",
                "RoleCode": self.role_code,
            },
        )
        if response.status_code == 401 or "sso.buaa.edu.cn" in str(response.url):
            raise AuthenticationRequired("SPOC 会话已失效")
        if response.status_code != 200:
            raise RuntimeError(f"SPOC 返回 {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("SPOC 返回了非 JSON 内容") from exc
        message = str(payload.get("msg") or payload.get("msg_en") or "SPOC 请求失败")
        if int(payload.get("code", -1)) != 200:
            if any(word in message.lower() for word in ("登录", "token", "未认证", "未登录", "权限")):
                raise AuthenticationRequired(message)
            raise RuntimeError(message)
        content = payload.get("content")
        if content is None:
            return []
        if not isinstance(content, list):
            raise RuntimeError("SPOC 课程字段结构已变化")
        return [item for item in content if isinstance(item, dict)]

    @staticmethod
    def extract_token(url: str) -> str | None:
        parsed = urlparse(url)
        if "/spocnew/cas" not in parsed.path:
            return None
        values = parse_qs(parsed.query)
        return (values.get("token") or [None])[0]

    @staticmethod
    def encrypt_param(plain: str) -> str:
        data = plain.encode()
        data += b"\0" * ((16 - len(data) % 16) % 16)
        encryptor = Cipher(
            algorithms.AES(b"inco12345678ocni"), modes.CBC(b"ocni12345678inco")
        ).encryptor()
        return base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode()

    @classmethod
    def _first_string(cls, value: Any) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            return next((found for item in value if (found := cls._first_string(item))), None)
        if isinstance(value, dict):
            return next((found for item in value.values() if (found := cls._first_string(item))), None)
        return None

    def _event(
        self, term_code: str, item: dict[str, Any],
        course_map: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        assignment_id = str(item.get("zyid") or "")
        title = str(item.get("zymc") or "SPOC 作业")
        course_id = str(item.get("sskcid") or "")
        course_info = (course_map or {}).get(course_id, {})
        course = str(item.get("kcmc") or course_info.get("kcmc") or "SPOC")
        status = self.submission_status(item.get("tjzt"), item.get("tjzt") is not None)
        submitted = status == "submitted"
        identity = assignment_id or hashlib.sha1(f"{term_code}|{course}|{title}".encode()).hexdigest()[:16]
        opens_at = self._date_time(item.get("zykssj"))
        return {
            "source": self.id,
            "external_id": f"assignment-{identity}",
            "kind": "assignment",
            "title": f"{course} · {title}",
            "starts_at": None,
            "due_at": self._date_time(item.get("zyjzsj")),
            "status": "done" if submitted else "pending" if status == "unsubmitted" else "unknown",
            "metadata": {
                **item, "term_code": term_code, "course_id": course_id,
                "teacher": course_info.get("skjs"), "opens_at": opens_at,
                "submitted": submitted, "max_score": item.get("mf"),
                "submission_status": status, "assignment_type": str(item.get("zylx") or ""),
                "course_name": course, "status_source": "assignment_list",
            },
        }

    @staticmethod
    def _date_time(value: Any) -> str | None:
        if not value:
            return None
        raw = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw.replace(" ", "T"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return parsed.isoformat()
