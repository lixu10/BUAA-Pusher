from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import padding as symmetric_padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.upstream.sso import AuthenticationRequired, BuaaSsoSession
from app.upstream.webvpn import from_webvpn_url

from .base import ConnectorManifest, SourceAdapter
from .text import plain_text


BASE_URL = "https://bykc.buaa.edu.cn"
LOGIN_URL = f"{BASE_URL}/sscv/cas/login"
FALLBACK_LOGIN_URL = f"{BASE_URL}/cas-login?token="
API_BASE = f"{BASE_URL}/sscv"
READ_ONLY_APIS = frozenset({"getAllConfig", "queryChosenCourse", "queryStudentSemesterCourseByPage", "queryCourseById"})
RSA_PUBLIC_KEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDlHMQ3B5GsWnCe7Nlo1YiG/YmHdlOiKOST5aRm4iaq"
    "YSvhvWmwcigoyWTM+8bv2+sf6nQBRDWTY4KmNV7DBk1eDnTIQo6ENA31k5/tYCLEXgjPbEjCK9spiyB"
    "62fCT6cqOhbamJB0lcDJRO6Vo1m3dy+fD0jbxfDVBBNtyltIsDQIDAQAB"
)
KEY_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"


@dataclass(frozen=True)
class EncryptedRequest:
    body: str
    ak: str
    sk: str
    aes_key: bytes


class BykcAdapter(SourceAdapter):
    id = "bykc"
    label = "博雅课程"
    cleanup_kinds = ("boya", "signin", "signout")
    manifest = ConnectorManifest(
        id=id,
        label=label,
        version="2026-10-13",
        capabilities=("chosen_courses", "course_schedule", "catalog", "course_details", "enrollment_windows", "signin_windows", "signout_windows"),
        endpoints=(
            LOGIN_URL,
            f"{API_BASE}/getAllConfig",
            f"{API_BASE}/queryChosenCourse",
            f"{API_BASE}/queryStudentSemesterCourseByPage", f"{API_BASE}/queryCourseById",
        ),
        references=(
            "BUAASubnet/UBAA:BykcClient.kt",
            "BUAASubnet/UBAA:BykcCrypto.kt",
            "BUAASubnet/UBAA:BykcService.kt",
            "fontlos/buaa-api:src/api/boya",
        ),
    )

    def __init__(self, session: BuaaSsoSession):
        self.session = session
        self.token = ""
        self.config = {}

    async def sync(self) -> list[dict[str, Any]]:
        if not self.session.authenticated:
            raise AuthenticationRequired("需要登录北航统一认证")
        await self._login()
        config = await self._call("getAllConfig", {})
        self.config = config
        semester = self.choose_semester(config.get("semester") or [])
        start, end = semester.get("semesterStartDate"), semester.get("semesterEndDate")
        if not start or not end:
            raise RuntimeError("博雅系统当前学期缺少起止日期")
        payload = await self._call("queryChosenCourse", {"startDate": start, "endDate": end})
        rows = payload.get("courseList")
        if not isinstance(rows, list):
            raise RuntimeError("博雅已选课程字段结构已变化")
        events: list[dict[str, Any]] = []
        for chosen in rows:
            if not isinstance(chosen, dict) or not isinstance(chosen.get("courseInfo"), dict):
                raise RuntimeError("博雅已报名课程详情缺失，旧数据已保留")
            self.report_progress(len(events), len(rows), "读取已报名课程")
            events.extend(self._events(chosen))
        self.report_progress(len(rows), len(rows), "读取已报名课程")
        return events

    async def catalog(self) -> list[dict[str, Any]]:
        if not self.session.authenticated:
            raise AuthenticationRequired("需要登录学校认证")
        await self._login()
        self.config = await self._call("getAllConfig", {})
        result, seen = [], set()
        page = 1
        while page <= 100:
            payload = await self._call("queryStudentSemesterCourseByPage", {"pageNumber": page, "pageSize": 50})
            rows, pages = payload.get("content"), self._int(payload.get("totalPages"))
            total = self._int(payload.get("totalElements"))
            if not isinstance(rows, list) or pages is None or pages < 0 or total is None or total < 0:
                raise RuntimeError("博雅目录分页结构已变化，旧数据已保留")
            self.report_progress(page - 1, max(pages, 1), "读取课程目录")
            for row in rows:
                if not isinstance(row, dict) or not str(row.get("id", "")).isdigit() or str(row["id"]) in seen:
                    raise RuntimeError("博雅目录课程缺失或分页重复，旧数据已保留")
                seen.add(str(row["id"]))
                result.append(self.catalog_event(row))
            if page >= pages:
                if len(result) != total:
                    raise RuntimeError("博雅目录分页未完整读取，旧数据已保留")
                self.report_progress(page, max(pages, 1), "读取课程目录")
                return result
            if not rows:
                break
            page += 1
        raise RuntimeError("博雅目录分页未完整读取，旧数据已保留")

    async def course_details(self, ids: list[str]) -> list[dict]:
        if not self.session.authenticated:
            raise AuthenticationRequired("需要登录学校认证")
        await self._login()
        self.config = await self._call("getAllConfig", {})
        result = []
        for course_id in ids:
            if not str(course_id).isdigit():
                raise ValueError("课程标识无效")
            raw = await self._call("queryCourseById", {"id": int(course_id)})
            if str(raw.get("id")) != str(course_id):
                raise RuntimeError("博雅详情课程不匹配")
            result.append(self.catalog_event(raw))
        return result

    def catalog_event(self, course: dict[str, Any]) -> dict:
        course_id = str(course.get("id") or "")
        if not course_id.isdigit():
            raise RuntimeError("博雅课程标识缺失")
        categories = [course.get(f"courseNewKind{i}") or course.get(f"courseKind{i}") for i in (1, 2, 3)]
        categories = [kind for kind in categories if isinstance(kind, dict) and kind.get("kindName")]
        campus_map = {str(c["id"]): str(c["campusName"]) for c in self.config.get("campus", []) if isinstance(c, dict) and c.get("id") is not None and c.get("campusName")}
        raw_campus = course.get("courseCampus")
        try:
            ids = json.loads(raw_campus) if isinstance(raw_campus, str) and raw_campus.startswith("[") else []
        except ValueError:
            ids = []
        names = course.get("courseCampusList")
        names = [str(v) for v in names if isinstance(v, str)] if isinstance(names, list) else []
        all_campuses = raw_campus == "ALL" or "全部校区" in names
        campus_ids = [str(v) for v in ids] if isinstance(ids, list) else []
        if not campus_ids and not all_campuses:
            campus_ids = [next((key for key, value in campus_map.items() if value == name), f"name:{name}") for name in names]
        campus_names = [campus_map.get(value, names[i] if i < len(names) else f"校区 {value}") for i, value in enumerate(campus_ids)]
        maximum, current = self._int(course.get("courseMaxCount")), self._int(course.get("courseCurrentCount"))
        if maximum is not None and maximum < 0 or current is not None and current < 0:
            raise RuntimeError("博雅人数数据无效")
        sign = self.parse_sign_config(course.get("courseSignConfig"))
        selected = course.get("selected")
        metadata = {"course_id": course_id, "teacher": self._text(course.get("courseTeacher")),
            "read_at": datetime.now(timezone.utc).isoformat(),
            "category_ids": [str(kind.get("id")) if kind.get("id") is not None else f"name:{kind['kindName']}" for kind in categories],
            "category_names": [str(kind["kindName"]) for kind in categories], "campus_ids": campus_ids,
            "campus_names": campus_names, "all_campuses": all_campuses, "max_count": maximum, "current_count": current,
            "remaining": max(0, maximum - current) if maximum is not None and current is not None else None,
            "selected": selected if type(selected) is bool else None,
            "sign_type": str(course["courseSignType"]) if course.get("courseSignType") is not None else None,
            "enrollment_starts_at": self._date_time(course.get("courseSelectStartDate")),
            "enrollment_ends_at": self._date_time(course.get("courseSelectEndDate")),
            "cancel_ends_at": self._date_time(course.get("courseCancelEndDate")),
            "description": plain_text(course.get("courseDesc")), "contact": self._text(course.get("courseContact")),
            "contact_phone": self._text(course.get("courseContactMobile")), "selection_mode": self._text(course.get("courseSelectType")),
            "department": (course.get("courseBelongCollege") or {}).get("collegeName"),
            "homework_description": plain_text(course.get("courseHomeworkRequirement")),
            "homework_starts_at": self._date_time(course.get("courseHomeworkStartDate")),
            "homework_ends_at": self._date_time(course.get("courseHomeworkEndDate")),
            "audience": {key: [value for value in course[key] if isinstance(value, str)] for key in ("courseCollegeList", "courseTermList", "courseGroupList") if isinstance(course.get(key), list)},
            "sign_windows": {key: self._date_time(sign.get(key)) for key in ("signStartDate", "signEndDate", "signOutStartDate", "signOutEndDate")},
            "school_url": f"{BASE_URL}/system/course-select"}
        return {"source": "bykc", "external_id": f"catalog-{course_id}", "kind": "boya_catalog",
            "title": self._text(course.get("courseName")) or "博雅课程", "starts_at": self._date_time(course.get("courseStartDate")),
            "ends_at": self._date_time(course.get("courseEndDate")), "location": self._text(course.get("coursePosition")),
            "status": "upcoming", "metadata": metadata}

    def _events(self, chosen):
        event = self.catalog_event(chosen["courseInfo"])
        event.update(external_id=f"course-{event['metadata']['course_id']}", kind="boya")
        event["metadata"].update({"selected": True, "chosen_id": chosen.get("id"),
            "enrolled_at": self._date_time(chosen.get("selectDate")), "checkin": self._int(chosen.get("checkin")),
            "pass": self._int(chosen.get("pass")), "score": chosen.get("score"), "sign_info": plain_text(chosen.get("signInfo")),
            "homework": plain_text(chosen.get("homework")), "homework_attachment_name": self._text(chosen.get("homeworkAttachmentName"))})
        return [event]

    async def _login(self) -> None:
        response = await self.session.client.get(LOGIN_URL)
        token = self.extract_token(str(response.url))
        if not token:
            token = self.extract_token(response.headers.get("location") or "")
        if token:
            self.token = token
            return
        if response.status_code in {401, 403} or "sso.buaa.edu.cn" in from_webvpn_url(str(response.url)):
            raise AuthenticationRequired("博雅系统授权未完成")
        try:
            await self.session.client.get(FALLBACK_LOGIN_URL)
        except Exception:
            pass

    async def _call(self, api_name: str, body: dict[str, Any]) -> dict[str, Any]:
        if api_name not in READ_ONLY_APIS:
            raise ValueError("博雅连接仅支持只读查询")
        plain = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        encrypted = self.encrypt_request(plain)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "Referer": f"{BASE_URL}/system/course-select",
            "Origin": BASE_URL,
            "ak": encrypted.ak,
            "sk": encrypted.sk,
            "ts": str(int(datetime.now().timestamp() * 1000)),
        }
        if self.token:
            headers.update({"auth_token": self.token, "authtoken": self.token})
        response = await self.session.client.post(
            f"{API_BASE}/{api_name}", content=encrypted.body, headers=headers
        )
        if response.status_code in {401, 403} or "sso.buaa.edu.cn" in from_webvpn_url(str(response.url)):
            raise AuthenticationRequired("博雅系统会话已失效")
        if response.status_code != 200:
            raise RuntimeError(f"博雅系统返回 {response.status_code}")
        decoded = self.decrypt_response(response.text, encrypted.aes_key)
        if "会话已失效" in decoded or "未登录" in decoded:
            raise AuthenticationRequired("博雅系统会话已失效")
        try:
            envelope = json.loads(decoded)
        except ValueError as exc:
            raise RuntimeError("博雅系统返回了无法解析的内容") from exc
        if not isinstance(envelope, dict):
            raise RuntimeError("博雅系统响应字段结构已变化")
        if str(envelope.get("status")) != "0" or not isinstance(envelope.get("data"), dict):
            raise RuntimeError(str(envelope.get("errmsg") or envelope.get("msg") or "博雅系统业务错误"))
        return envelope["data"]


    @staticmethod
    def choose_semester(rows: list[dict[str, Any]], today: date | None = None) -> dict[str, Any]:
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("博雅系统未返回学期配置")
        today = today or datetime.now(timezone(timedelta(hours=8))).date()
        for row in rows:
            if not isinstance(row, dict):
                continue
            start = BykcAdapter._date(row.get("semesterStartDate"))
            end = BykcAdapter._date(row.get("semesterEndDate"))
            if start and end and start <= today <= end:
                return row
        valid = [row for row in rows if isinstance(row, dict)]
        return max(valid, key=lambda row: BykcAdapter._date(row.get("semesterEndDate")) or date.min)

    @staticmethod
    def parse_sign_config(value: Any) -> dict[str, Any]:
        if not isinstance(value, str) or not value.strip():
            return {}
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except ValueError:
            return {}

    @staticmethod
    def extract_token(url: str) -> str | None:
        values = parse_qs(urlparse(url).query)
        return (values.get("token") or [None])[0]

    @staticmethod
    def encrypt_request(plain: str) -> EncryptedRequest:
        data = plain.encode("utf-8")
        aes_key = "".join(secrets.choice(KEY_CHARS) for _ in range(16)).encode("utf-8")
        public_key = serialization.load_der_public_key(base64.b64decode(RSA_PUBLIC_KEY))
        ak = base64.b64encode(public_key.encrypt(aes_key, asymmetric_padding.PKCS1v15())).decode()
        digest = hashlib.sha1(data).hexdigest().encode("utf-8")
        sk = base64.b64encode(public_key.encrypt(digest, asymmetric_padding.PKCS1v15())).decode()
        body = base64.b64encode(BykcAdapter._aes_encrypt(data, aes_key)).decode()
        return EncryptedRequest(body=body, ak=ak, sk=sk, aes_key=aes_key)

    @staticmethod
    def decrypt_response(body: str, aes_key: bytes) -> str:
        text = body.strip()
        try:
            value = json.loads(text)
            encoded = value if isinstance(value, str) else text
        except ValueError:
            encoded = text
        try:
            return BykcAdapter._aes_decrypt(base64.b64decode(encoded), aes_key).decode("utf-8")
        except Exception:
            return encoded

    @staticmethod
    def _aes_encrypt(data: bytes, key: bytes) -> bytes:
        padder = symmetric_padding.PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return encryptor.update(padded) + encryptor.finalize()

    @staticmethod
    def _aes_decrypt(data: bytes, key: bytes) -> bytes:
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = decryptor.update(data) + decryptor.finalize()
        unpadder = symmetric_padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()

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

    @staticmethod
    def _date(value: Any) -> date | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).strip().replace(" ", "T")).date()
        except ValueError:
            return None

    @staticmethod
    def _int(value: Any) -> int | None:
        if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
            return None
        try:
            return int(value) if value is not None and str(value).strip() else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _text(value: Any) -> str | None:
        return str(value).strip() if value is not None and str(value).strip() else None
