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

from .base import ConnectorManifest, SourceAdapter


BASE_URL = "https://bykc.buaa.edu.cn"
LOGIN_URL = f"{BASE_URL}/sscv/cas/login"
FALLBACK_LOGIN_URL = f"{BASE_URL}/cas-login?token="
API_BASE = f"{BASE_URL}/sscv"
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
    manifest = ConnectorManifest(
        id=id,
        label=label,
        version="2026-09-19",
        capabilities=("chosen_courses", "course_schedule", "signin_windows", "signout_windows"),
        endpoints=(
            LOGIN_URL,
            f"{API_BASE}/getAllConfig",
            f"{API_BASE}/queryChosenCourse",
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

    async def sync(self) -> list[dict[str, Any]]:
        if not self.session.authenticated:
            raise AuthenticationRequired("需要登录北航统一认证")
        await self._login()
        config = await self._call("getAllConfig", {})
        semester = self.choose_semester(config.get("semester") or [])
        start, end = semester.get("semesterStartDate"), semester.get("semesterEndDate")
        if not start or not end:
            raise RuntimeError("博雅系统当前学期缺少起止日期")
        payload = await self._call("queryChosenCourse", {"startDate": start, "endDate": end})
        rows = payload.get("courseList") or []
        if not isinstance(rows, list):
            raise RuntimeError("博雅已选课程字段结构已变化")
        events: list[dict[str, Any]] = []
        for chosen in rows:
            if not isinstance(chosen, dict) or not isinstance(chosen.get("courseInfo"), dict):
                continue
            events.extend(self._events(chosen))
        return events

    async def _login(self) -> None:
        response = await self.session.client.get(LOGIN_URL)
        token = self.extract_token(str(response.url))
        if not token:
            token = self.extract_token(response.headers.get("location") or "")
        if token:
            self.token = token
            return
        if response.status_code in {401, 403} or "sso.buaa.edu.cn" in str(response.url):
            raise AuthenticationRequired("博雅系统授权未完成")
        try:
            await self.session.client.get(FALLBACK_LOGIN_URL)
        except Exception:
            pass

    async def _call(self, api_name: str, body: dict[str, Any]) -> dict[str, Any]:
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
        if response.status_code in {401, 403} or "sso.buaa.edu.cn" in str(response.url):
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

    def _events(self, chosen: dict[str, Any]) -> list[dict[str, Any]]:
        course = chosen["courseInfo"]
        course_id = str(course.get("id") or chosen.get("id") or "")
        name = str(course.get("courseName") or "博雅课程").strip()
        checkin, passed = self._int(chosen.get("checkin")), self._int(chosen.get("pass"))
        location = self._text(course.get("coursePosition"))
        metadata = {
            "chosen_id": chosen.get("id"),
            "course_id": course.get("id"),
            "teacher": course.get("courseTeacher"),
            "checkin": checkin,
            "pass": passed,
            "score": chosen.get("score"),
            "sign_info": chosen.get("signInfo"),
            "course_sign_type": course.get("courseSignType"),
        }
        course_event = {
            "source": self.id,
            "external_id": f"course-{course_id}",
            "kind": "boya",
            "title": name,
            "starts_at": self._date_time(course.get("courseStartDate")),
            "ends_at": self._date_time(course.get("courseEndDate")),
            "location": location,
            "status": "done" if passed == 1 else "upcoming",
            "metadata": metadata,
        }
        events = [course_event]
        sign_config = self.parse_sign_config(course.get("courseSignConfig"))
        sign_start, sign_end = sign_config.get("signStartDate"), sign_config.get("signEndDate")
        if sign_start or sign_end:
            signed = checkin not in {None, 0}
            events.append({
                "source": self.id,
                "external_id": f"signin-{course_id}",
                "kind": "signin",
                "title": f"{name}签到",
                "starts_at": self._date_time(sign_start),
                "ends_at": self._date_time(sign_end),
                "location": location,
                "status": "done" if signed else "missing",
                "metadata": {**metadata, "signed": signed, "window": "signin"},
            })
        out_start = sign_config.get("signOutStartDate")
        out_end = sign_config.get("signOutEndDate")
        if out_start or out_end:
            eligible = passed != 1 and checkin in {None, 0, 5, 6}
            events.append({
                "source": self.id,
                "external_id": f"signout-{course_id}",
                "kind": "signout",
                "title": f"{name}签退",
                "starts_at": self._date_time(out_start),
                "ends_at": self._date_time(out_end),
                "location": location,
                "status": "missing" if eligible else "done",
                "metadata": {**metadata, "signed_out": not eligible, "window": "signout"},
            })
        return events

    @staticmethod
    def choose_semester(rows: list[dict[str, Any]], today: date | None = None) -> dict[str, Any]:
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("博雅系统未返回学期配置")
        today = today or date.today()
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
        try:
            return int(value) if value is not None and str(value).strip() else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _text(value: Any) -> str | None:
        return str(value).strip() if value is not None and str(value).strip() else None
