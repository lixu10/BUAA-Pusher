"""Shared display and reminder semantics; unknown never means submitted."""
from datetime import UTC, datetime
from typing import Any


def completion_state(event: dict[str, Any]) -> str:
    if event.get("kind") == "course":
        return "not_applicable"
    metadata = event.get("metadata") or {}
    state = metadata.get("submission_status")
    if state in {"submitted", "unsubmitted", "partial", "unknown"}:
        return state
    if event.get("status") == "done":
        return "submitted" if event.get("kind") == "assignment" else "completed"
    return {"pending": "unsubmitted", "missing": "unsubmitted", "partial": "partial"}.get(
        event.get("status"), "unknown"
    )


def expired(event: dict[str, Any], now: datetime | None = None) -> bool:
    value = event.get("due_at") if event.get("kind") == "assignment" else event.get("ends_at")
    if not value:
        return False
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        timestamp = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp
        return timestamp < (now or datetime.now(UTC))
    except (ValueError, TypeError):
        return False


def public_event(event: dict[str, Any]) -> dict[str, Any]:
    state = completion_state(event)
    label = {"submitted": "已提交", "completed": "已签到", "unsubmitted": "未提交",
             "partial": "部分提交", "unknown": "状态未知", "not_applicable": "待上课"}[state]
    if event.get("kind") == "course":
        starts = event.get("starts_at")
        if expired(event):
            label = "已结束"
        elif starts:
            try:
                value = datetime.fromisoformat(starts.replace("Z", "+00:00"))
                value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
                if value <= datetime.now(UTC):
                    label = "上课中"
            except (ValueError, TypeError):
                pass
    if event.get("kind") == "signin" and state == "unsubmitted":
        label = "未签到"
    return {**event, "ignored": bool(event.get("ignored")), "completion_state": state,
            "status_label": "已忽略" if event.get("ignored") else label, "expired": expired(event)}


def reminder_matches(rule: dict[str, Any], event: dict[str, Any], now: datetime | None = None) -> bool:
    if event.get("ignored"):
        return False
    if event.get("kind") == "course":
        return not expired(event, now)
    if event.get("kind") not in {"assignment", "signin"}:
        return True
    settings = rule.get("profile_settings") or {}
    if expired(event, now):
        return False
    state = completion_state(event)
    mode = settings.get("completion_filter", "incomplete")
    if mode == "incomplete" and state in {"submitted", "completed"}:
        return False
    if mode == "incomplete" and event.get("kind") == "signin" and state == "unknown":
        return False
    if mode not in {"any", "incomplete"} and state != mode:
        return False
    assignment_type = settings.get("assignment_type", "")
    return not assignment_type or (event.get("metadata") or {}).get("assignment_type") == assignment_type
