from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from app.upstream.sso import AuthenticationRequired, BuaaSsoSession

from .base import ConnectorManifest, SourceAdapter
from .byxt import ByxtAdapter


SCORE_URL = "https://app.buaa.edu.cn/buaascore/wap/default/index"


class GradeAdapter(SourceAdapter):
    id = "grade"
    label = "课程成绩"
    manifest = ConnectorManifest(
        id=id,
        label=label,
        version="2026-09-19",
        capabilities=("grades", "grade_changes"),
        endpoints=(SCORE_URL,),
        references=(
            "BUAASubnet/UBAA:GradeService.kt",
            "BUAASubnet/UBAA:Grade.kt",
            "fontlos/buaa-api:src/api/app",
        ),
    )

    def __init__(self, session: BuaaSsoSession):
        self.session = session

    async def sync(self) -> list[dict[str, Any]]:
        byxt = ByxtAdapter(self.session)
        await byxt._ensure_login()
        terms = (await byxt._json("GET", "api/home/student/schoolCalendars.do")).get("datas", [])
        term = next((item for item in terms if item.get("selected")), terms[0] if terms else None)
        if not term:
            return []
        term_code = str(term.get("itemCode") or "")
        year, semester = self._term_parts(term_code)

        activation = await self.session.client.get(SCORE_URL)
        self._ensure_response(activation)
        response = await self.session.client.post(
            SCORE_URL,
            data={"xq": str(semester), "year": year},
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": SCORE_URL,
            },
        )
        self._ensure_response(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("成绩接口返回了非 JSON 内容") from exc
        if int(payload.get("e", -1)) != 0:
            raise RuntimeError(str(payload.get("m") or "成绩接口业务错误"))
        courses = payload.get("d") or {}
        if not isinstance(courses, dict):
            raise RuntimeError("成绩接口字段结构已变化")
        return [self._event(term_code, key, item) for key, item in courses.items() if isinstance(item, dict)]

    @staticmethod
    def _term_parts(term_code: str) -> tuple[str, int]:
        parts = term_code.split("-")
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit() or not parts[2].isdigit():
            raise RuntimeError(f"不支持的学期代码：{term_code}")
        return f"{parts[0]}-{parts[1]}", int(parts[2])

    @staticmethod
    def _ensure_response(response: Any) -> None:
        url, body = str(response.url), response.text.lstrip()
        if response.status_code == 401 or "sso.buaa.edu.cn" in url or "name=\"execution\"" in response.text:
            raise AuthenticationRequired("成绩系统授权未完成")
        if response.status_code != 200:
            raise RuntimeError(f"成绩系统返回 {response.status_code}")
        if body.lower().startswith("<html") and "统一身份认证" in response.text:
            raise AuthenticationRequired("成绩系统授权未完成")

    def _event(self, term_code: str, key: str, item: dict[str, Any]) -> dict[str, Any]:
        name = str(item.get("kcmc") or "课程")
        course_code = str(item.get("kch") or key)
        score = self._text(item.get("kccj"))
        identity = hashlib.sha1(f"{term_code}|{course_code}|{name}".encode()).hexdigest()[:16]
        return {
            "source": self.id,
            "external_id": f"grade-{identity}",
            "kind": "grade",
            "title": f"{name} 成绩",
            "starts_at": datetime.now(UTC).isoformat(),
            "status": "published",
            "metadata": {
                "term_code": term_code,
                "course_name": name,
                "course_code": course_code,
                "score": score,
                "credit": self._text(item.get("xf")),
                "score_type": item.get("fslx"),
                "course_type": item.get("kclx"),
            },
            "_change_event": True,
        }

    @staticmethod
    def _text(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, dict):
            value = value.get("value") or value.get("text")
        return str(value).strip() or None
