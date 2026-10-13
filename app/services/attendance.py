"""Readonly attendance cache and checkpoint execution; never performs check-in."""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime, timedelta, timezone

from app.db import now_iso

CHINA = timezone(timedelta(hours=8))


def parsed(value):
    try:
        time = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return time.replace(tzinfo=UTC) if time.tzinfo is None else time.astimezone(UTC)
    except (ValueError, TypeError, AttributeError):
        return None


def course_name(event):
    return re.sub(r"\s+", "", str((event.get("metadata") or {}).get("course_name") or event.get("title") or ""))


def slot(event):
    start, end = parsed(event.get("starts_at")), parsed(event.get("ends_at"))
    return f"{course_name(event)}|{start.isoformat()}|{end.isoformat()}" if start and end else None


def cached_status(course):
    value = (course.get("metadata") or {}).get("_attendance") or {}
    return value.get("status", "unknown") if value.get("slot") == slot(course) else "unknown"


def merge_calendar(events, enabled=True):
    merged = []
    for event in events:
        if event.get("kind") == "signin":
            continue
        item = dict(event)
        item.pop("attendance", None)
        cached = (event.get("metadata") or {}).get("_attendance") or {}
        if enabled and event.get("source") == "byxt" and event.get("kind") == "course" and slot(event) and cached.get("slot") == slot(event):
            item["attendance"] = cached
        merged.append(item)
    return merged


def plan_checks(db, user_id, rule, courses, now):
    excluded = {re.sub(r"\s+", "", name) for name in rule.get("profile_settings", {}).get("excluded_courses", [])}
    plans = []
    queued_keys = db.queued_trigger_keys(user_id, rule["id"])
    for course in courses:
        if course.get("source") != "byxt" or course.get("kind") != "course" or course.get("ignored") or course_name(course) in excluded:
            continue
        start, end = parsed(course.get("starts_at")), parsed(course.get("ends_at"))
        if not start or not end or start >= end:
            continue
        if cached_status(course) == "done":
            db.cancel_course_checks(user_id, rule["id"], course["id"])
            continue
        points = set()
        for point in rule["profile_settings"]["check_times"]:
            anchor = start if point["anchor"] == "starts_at" else end
            at = anchor + timedelta(minutes=point["offset_minutes"])
            if start - timedelta(minutes=10) <= at <= end and end >= now:
                points.add(at)
        for at in sorted(points):
            check_key = hashlib.sha256(f"{user_id}:{rule['id']}:{slot(course)}:{at.isoformat()}".encode()).hexdigest()
            for index, _ in enumerate(rule["actions"]):
                dedupe_key = f"attendance:{check_key}:{index}"
                if at < now - timedelta(seconds=30) and dedupe_key not in queued_keys:
                    continue
                plans.append({"event_id": course["id"], "action_index": index, "occurrence": 1,
                    "run_at": at.isoformat(), "check_key": check_key,
                    "dedupe_key": dedupe_key})
    return db.replace_triggers_for_rule(user_id, rule["id"], plans)


class AttendanceService:
    def __init__(self, db, query_day):
        self.db, self.query_day = db, query_day
        self.locks = {}
        self.day_reads = {}

    def record(self, user_id, records):
        courses = [e for e in self.db.list_events(user_id, limit=5000) if e["source"] == "byxt" and e["kind"] == "course"]
        groups = {}
        for course in courses:
            if slot(course):
                groups.setdefault(slot(course), []).append(course)
        record_groups = {}
        for record in records:
            if slot(record):
                record_groups.setdefault(slot(record), []).append(record)
        for key, rows in record_groups.items():
            candidates = groups.get(key, [])
            if len(candidates) != 1 or len(rows) != 1:
                continue  # Never guess ambiguous course mappings.
            course, record = candidates[0], rows[0]
            status = record["status"]
            if cached_status(course) == "done":
                status = "done"  # Confirmation for this exact instance is monotonic.
            metadata = {**course["metadata"], "_attendance": {"status": status,
                "slot": key, "checked_at": now_iso(), "external_id": record["external_id"]}}
            self.db.upsert_event(user_id, {**course, "metadata": metadata})

    def details(self, user_id):
        courses = [e for e in self.db.list_events(user_id) if e["source"] == "byxt" and e["kind"] == "course"]
        return [{"title": (e["metadata"] or {}).get("course_name", e["title"]),
                 "starts_at": e["starts_at"], "ends_at": e["ends_at"], "status": e["status"],
                 "matched": sum(slot(c) == slot(e) for c in courses) == 1}
                for e in self.db.list_events(user_id) if e["source"] == "iclass" and e["kind"] == "signin"]

    async def prepare(self, user_id, context, event, definition):
        key = context.get("check_key")
        if not key:
            return "unknown", "旧签到规则需要重新设置检查点"
        async with self.locks.setdefault(user_id, asyncio.Lock()):
            latest = self.db.trigger_context(context["id"])
            if not latest or latest["user_id"] != user_id or latest["status"] != "queued" or not latest["rule_enabled"] or latest["definition"] != definition or not (
                    self.db.get_source(user_id, "iclass") or {}).get("enabled"):
                return "unknown", "课程检查或连接已停用"
            course = self.db.event_by_id(user_id, context["event_id"])
            if not course or course.get("ignored"):
                return "unknown", "课程不存在或已忽略"
            excluded = {re.sub(r"\s+", "", name) for name in definition.get("profile_settings", {}).get("excluded_courses", [])}
            if course_name(course) in excluded:
                return "unknown", "课程已排除"
            if cached_status(course) == "done":
                self.db.cancel_course_checks(user_id, context["rule_id"], course["id"])
                return "done", "已签到，后续检查取消"
            previous = self.db.attendance_check_result(user_id, key)
            # A missing result is reusable only for this delivery time. A retry
            # or quiet-hour delay has a later run_at and must query again.
            observed = parsed(previous["created_at"]) if previous else None
            if observed and observed >= parsed(context["run_at"]) and datetime.now(UTC) - observed <= timedelta(seconds=30):
                return previous["result"], previous["detail"]
            now, start, end = datetime.now(UTC), parsed(course["starts_at"]), parsed(course["ends_at"])
            if not start or not end or not start - timedelta(minutes=10) <= now <= end + timedelta(seconds=30):
                return "unknown", "已超出课程检查时间"
            if parsed(context["run_at"]) < now - timedelta(seconds=60):
                return "unknown", "检查点已过期，不补发"
            result, detail = "unknown", "未发现该课程的签到记录"
            try:
                day = start.astimezone(CHINA).date()
                mode = (self.db.get_source(user_id, "iclass") or {}).get("network_mode", "direct")
                query_key = (user_id, day.isoformat(), mode, context["run_at"])
                cached_read = self.day_reads.get(query_key)
                if not cached_read or datetime.now(UTC) - cached_read["at"] > timedelta(seconds=30):
                    if len(self.day_reads) >= 64:
                        self.day_reads.pop(next(iter(self.day_reads)))
                    try:
                        records = await self.query_day(user_id, day)
                    except Exception:
                        records = None
                    self.day_reads[query_key] = {"at": datetime.now(UTC), "records": records}
                records = self.day_reads[query_key]["records"]
                if records is None:
                    raise RuntimeError("签到查询失败")
                matches = [r for r in records if slot(r) == slot(course)]
                # Cache records and attach statuses without rebuilding course schedules.
                for record in records:
                    self.db.upsert_event(user_id, record)
                self.record(user_id, records)
                if len(matches) == 1:
                    result = cached_status(self.db.event_by_id(user_id, course["id"]))
                    detail = "已签到" if result == "done" else "未签到" if result == "missing" else "签到状态未知"
            except Exception:
                result, detail = "error", "签到状态查询失败，未发送提醒；下个检查点再查"
            self.db.record_attendance_check(user_id, key, result, detail)
            latest = self.db.trigger_context(context["id"])
            current = self.db.event_by_id(user_id, course["id"])
            excluded = {re.sub(r"\s+", "", name) for name in
                ((latest or {}).get("definition", {}).get("profile_settings") or {}).get("excluded_courses", [])}
            if not latest or latest["status"] != "queued" or latest["definition"] != definition or not latest["rule_enabled"] or not current or current.get("ignored") or course_name(current) in excluded or not (
                    self.db.get_source(user_id, "iclass") or {}).get("enabled") or slot(current) != slot(course):
                return "unknown", "检查期间课程或规则已更改，未发送提醒"
            if result == "done":
                self.db.cancel_course_checks(user_id, context["rule_id"], course["id"])
            return result, detail
