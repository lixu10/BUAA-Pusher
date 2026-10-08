from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from app.db import Database
from app.features import CHANGE_PROFILES, event_available, rule_available
from app.services.sync_changes import change_matches
from app.services.event_state import reminder_matches
from app.services.academic import academic_window, in_academic_window


class RuleEngine:
    """Plans idempotent triggers from user-authored structured rules."""

    def __init__(self, db: Database, enforce_feature_scope: bool = False):
        self.db = db
        self.enforce_feature_scope = enforce_feature_scope

    def plan_user(self, user_id: int, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        inserted = 0
        events = self.db.list_events(user_id, limit=5000)
        if self.enforce_feature_scope:
            window = academic_window(self.db.preferences(user_id), now)
            events = [event for event in events if event_available(event)
                      and in_academic_window(event, window)
                      and (self.db.get_source(user_id, event["source"]) or {}).get("enabled", True)]
        for rule in self.db.list_automation_rules(user_id):
            if self.enforce_feature_scope and not rule_available(rule):
                self.db.cancel_rule_triggers(user_id, rule["id"], "Coming Soon")
                continue
            if not rule["enabled"]:
                self.db.cancel_rule_triggers(user_id, rule["id"], "规则已停用")
                continue
            if rule.get("profile") in CHANGE_PROFILES:
                # Change triggers have their own durable queue, so removal of a
                # cancelled course cannot erase its cancellation notification.
                self.db.replace_triggers_for_rule(user_id, rule["id"], [])
                for change in self.db.list_sync_changes(user_id):
                    if rule["id"] not in change["eligible_rules"] or rule["updated_at"] > change["detected_at"]:
                        continue
                    payload = change["payload"]
                    event = payload["event"]
                    current = self.db.event_by_key(user_id, event["source"], event["external_id"])
                    event = {**event, "ignored": (current or event).get("ignored", False)}
                    if not self.matches_scope(rule["scope"], event) or not change_matches(rule, payload, event):
                        continue
                    if self.enforce_feature_scope and (not event_available(event)
                            or not (self.db.get_source(user_id, event["source"]) or {}).get("enabled", True)
                            or not in_academic_window(event, window)):
                        continue
                    detected = self.parse_time(change["detected_at"])
                    for action_index, action in enumerate(rule["actions"]):
                        run_at = detected + timedelta(minutes=action.get("after_minutes", 0))
                        inserted += self.db.queue_change_trigger(user_id, rule["id"], change["id"],
                                                                 action_index, run_at.isoformat())
                continue
            planned: list[dict[str, Any]] = []
            for event in events:
                if not self.matches_scope(rule["scope"], event):
                    continue
                if not reminder_matches(rule, event, now):
                    continue
                triggers = rule.get("triggers") or ([rule["trigger"]] if rule.get("trigger") else [])
                for trigger_index, trigger in enumerate(triggers):
                    anchor = self.parse_time(event.get(trigger["anchor"]))
                    if not anchor:
                        continue
                    base = anchor + timedelta(minutes=trigger["offset_minutes"])
                    repeat = rule.get("repeat") or {}
                    every = repeat.get("every_minutes")
                    max_count = repeat.get("max_count", 1) if every else 1
                    until = self.parse_time(event.get(repeat.get("until_anchor"))) if repeat.get("until_anchor") else None
                    for occurrence in range(1, max_count + 1):
                        repeated_base = base + timedelta(minutes=(occurrence - 1) * every) if every else base
                        if until and repeated_base > until:
                            break
                        for action_index, action in enumerate(rule["actions"]):
                            run_at = repeated_base + timedelta(minutes=action.get("after_minutes", 0))
                            if event.get("kind") == "assignment":
                                deadline = self.parse_time(event.get("due_at"))
                                if deadline and run_at >= deadline:
                                    continue
                            if run_at < now - timedelta(minutes=5):
                                continue
                            raw_key = f"{user_id}:{rule['id']}:{event['id']}:{trigger_index}:{action_index}:{occurrence}:{run_at.isoformat()}"
                            planned.append({
                                "event_id": event["id"], "action_index": action_index,
                                "occurrence": occurrence, "run_at": run_at.isoformat(),
                                "dedupe_key": hashlib.sha256(raw_key.encode()).hexdigest(),
                            })
            inserted += self.db.replace_triggers_for_rule(user_id, rule["id"], planned)
        return inserted

    @staticmethod
    def matches_scope(scope: dict[str, Any], event: dict[str, Any]) -> bool:
        kinds = scope.get("kinds") or []
        sources = scope.get("sources") or []
        return (not kinds or event.get("kind") in kinds) and (not sources or event.get("source") in sources)

    @classmethod
    def conditions_match(cls, conditions: list[dict[str, Any]], event: dict[str, Any]) -> bool:
        return all(cls.condition_matches(condition, event) for condition in conditions)

    @staticmethod
    def condition_matches(condition: dict[str, Any], event: dict[str, Any]) -> bool:
        value: Any = event
        for part in condition["field"].split("."):
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
        expected, operator = condition.get("value"), condition["op"]
        if operator == "eq": return value == expected
        if operator == "ne": return value != expected
        if operator == "in": return value in (expected or [])
        if operator == "not_in": return value not in (expected or [])
        if operator == "exists": return value is not None
        if operator == "not_exists": return value is None
        if operator in {"gt", "gte", "lt", "lte"}:
            try:
                left, right = float(value), float(expected)
            except (TypeError, ValueError):
                return False
            if operator == "gt": return left > right
            if operator == "gte": return left >= right
            if operator == "lt": return left < right
            return left <= right
        if operator in {"contains", "not_contains"}:
            try:
                matched = expected in value
            except TypeError:
                matched = str(expected) in str(value) if value is not None else False
            return matched if operator == "contains" else not matched
        return False

    @staticmethod
    def parse_time(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        except ValueError:
            return None


RULE_TEMPLATES = [
    {
        "id": "course-start", "name": "上课提醒",
        "scope": {"kinds": ["course"], "sources": ["byxt"]},
        "trigger": {"anchor": "starts_at", "offset_minutes": -15},
        "conditions": [], "cancel_conditions": [],
        "repeat": {"every_minutes": None, "max_count": 1, "until_anchor": None},
        "actions": [{"channel": "in_app", "channel_id": None, "after_minutes": 0}],
        "quiet_hours_policy": "ignore",
    },
    {
        "id": "signin-escalation", "name": "未签到分级提醒",
        "scope": {"kinds": ["signin"], "sources": ["iclass"]},
        "trigger": {"anchor": "ends_at", "offset_minutes": -30},
        "conditions": [{"field": "status", "op": "eq", "value": "missing"}],
        "cancel_conditions": [{"field": "status", "op": "eq", "value": "done"}],
        "repeat": {"every_minutes": None, "max_count": 1, "until_anchor": "ends_at"},
        "actions": [
            {"channel": "in_app", "channel_id": None, "after_minutes": 0},
            {"channel": "in_app", "channel_id": None, "after_minutes": 20},
        ],
        "quiet_hours_policy": "ignore",
    },
    {
        "id": "assignment-due", "name": "作业截止提醒",
        "scope": {"kinds": ["assignment"], "sources": []},
        "trigger": {"anchor": "due_at", "offset_minutes": -1440},
        "conditions": [{"field": "status", "op": "ne", "value": "done"}],
        "cancel_conditions": [{"field": "status", "op": "eq", "value": "done"}],
        "repeat": {"every_minutes": None, "max_count": 1, "until_anchor": "due_at"},
        "actions": [
            {"channel": "in_app", "channel_id": None, "after_minutes": 0},
            {"channel": "in_app", "channel_id": None, "after_minutes": 1320},
        ],
        "quiet_hours_policy": "delay",
    },
    {
        "id": "exam-start", "name": "考试分级提醒",
        "scope": {"kinds": ["exam"], "sources": ["byxt"]},
        "trigger": {"anchor": "starts_at", "offset_minutes": -10080},
        "conditions": [], "cancel_conditions": [],
        "repeat": {"every_minutes": None, "max_count": 1, "until_anchor": "starts_at"},
        "actions": [
            {"channel": "in_app", "channel_id": None, "after_minutes": 0},
            {"channel": "in_app", "channel_id": None, "after_minutes": 8640},
            {"channel": "in_app", "channel_id": None, "after_minutes": 9960},
        ],
        "quiet_hours_policy": "ignore",
    },
    {
        "id": "grade-published", "name": "新成绩提醒",
        "scope": {"kinds": ["grade"], "sources": ["grade"]},
        "trigger": {"anchor": "starts_at", "offset_minutes": 0},
        "conditions": [{"field": "status", "op": "eq", "value": "published"}],
        "cancel_conditions": [],
        "repeat": {"every_minutes": None, "max_count": 1, "until_anchor": None},
        "actions": [{"channel": "in_app", "channel_id": None, "after_minutes": 0}],
        "quiet_hours_policy": "delay",
    },
    {
        "id": "fitness-deadline", "name": "阳光体育达标风险提醒",
        "scope": {"kinds": ["fitness"], "sources": ["ygdk"]},
        "trigger": {"anchor": "starts_at", "offset_minutes": 540},
        "conditions": [{"field": "metadata.must_max_every_week", "op": "eq", "value": True}],
        "cancel_conditions": [{"field": "status", "op": "eq", "value": "done"}],
        "repeat": {"every_minutes": 10080, "max_count": 40, "until_anchor": "due_at"},
        "actions": [{"channel": "in_app", "channel_id": None, "after_minutes": 0}],
        "quiet_hours_policy": "delay",
    },
    {
        "id": "library-booking", "name": "图书馆预约提醒",
        "scope": {"kinds": ["booking"], "sources": ["libbook"]},
        "trigger": {"anchor": "starts_at", "offset_minutes": -30},
        "conditions": [{"field": "status", "op": "ne", "value": "done"}],
        "cancel_conditions": [{"field": "status", "op": "eq", "value": "done"}],
        "repeat": {"every_minutes": None, "max_count": 1, "until_anchor": "starts_at"},
        "actions": [{"channel": "in_app", "channel_id": None, "after_minutes": 0}],
        "quiet_hours_policy": "ignore",
    },
    {
        "id": "boya-course", "name": "博雅上课提醒",
        "scope": {"kinds": ["boya"], "sources": ["bykc"]},
        "trigger": {"anchor": "starts_at", "offset_minutes": -30},
        "conditions": [{"field": "status", "op": "ne", "value": "done"}],
        "cancel_conditions": [],
        "repeat": {"every_minutes": None, "max_count": 1, "until_anchor": "starts_at"},
        "actions": [{"channel": "in_app", "channel_id": None, "after_minutes": 0}],
        "quiet_hours_policy": "ignore",
    },
    {
        "id": "boya-signout", "name": "博雅签退提醒",
        "scope": {"kinds": ["signout"], "sources": ["bykc"]},
        "trigger": {"anchor": "ends_at", "offset_minutes": -20},
        "conditions": [{"field": "status", "op": "ne", "value": "done"}],
        "cancel_conditions": [{"field": "status", "op": "eq", "value": "done"}],
        "repeat": {"every_minutes": 10, "max_count": 2, "until_anchor": "ends_at"},
        "actions": [{"channel": "in_app", "channel_id": None, "after_minutes": 0}],
        "quiet_hours_policy": "ignore",
    },
    {
        "id": "venue-booking", "name": "体育场馆预约提醒",
        "scope": {"kinds": ["venue"], "sources": ["cgyy"]},
        "trigger": {"anchor": "starts_at", "offset_minutes": -60},
        "conditions": [{"field": "status", "op": "in", "value": ["upcoming", "pending", "confirmed"]}],
        "cancel_conditions": [{"field": "status", "op": "in", "value": ["cancelled", "rejected"]}],
        "repeat": {"every_minutes": None, "max_count": 1, "until_anchor": "starts_at"},
        "actions": [{"channel": "in_app", "channel_id": None, "after_minutes": 0}],
        "quiet_hours_policy": "ignore",
    },
]
