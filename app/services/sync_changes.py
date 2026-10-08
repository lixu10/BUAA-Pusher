"""Read-only sync comparisons. Snapshots are separate from calendar entries."""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from app.db import Database

CHANGE_LABELS = {"time": "时间变更", "location": "教室变更", "teacher": "教师变更",
                 "title": "课程名称变更", "added": "新增课程", "cancelled": "课程取消"}


def course_fields(event: dict[str, Any]) -> dict[str, Any]:
    return {key: event.get(key) or "" for key in ("starts_at", "ends_at", "location", "title")} | {
        "teacher": (event.get("metadata") or {}).get("teacher") or ""}


def course_group(event: dict[str, Any]) -> tuple[str, ...]:
    meta = event.get("metadata") or {}
    # The upstream occurrence id includes the date/section and changes on a move.
    # Match unmatched occurrences only when one lesson remains in this course/week.
    start = str(event.get("starts_at") or "")
    week = meta.get("week_start")
    if not week and start:
        from datetime import timedelta
        day = datetime.fromisoformat(start).date()
        week = (day - timedelta(days=day.weekday())).isoformat()
    return tuple(str(value or "") for value in (meta.get("term_code"),
        meta.get("courseCode") or event.get("title"), meta.get("courseSerialNo"), week))


def compare_courses(previous: dict[str, dict], current: dict[str, dict]) -> list[dict]:
    changes = []
    pairs = [(previous[key], current[key]) for key in previous.keys() & current.keys()]
    old_groups, new_groups = defaultdict(list), defaultdict(list)
    for key in previous.keys() - current.keys():
        old_groups[course_group(previous[key])].append(previous[key])
    for key in current.keys() - previous.keys():
        new_groups[course_group(current[key])].append(current[key])
    for group in old_groups.keys() | new_groups.keys():
        old, new = old_groups[group], new_groups[group]
        if len(old) == len(new) == 1:
            pairs.append((old[0], new[0]))
        else:
            changes.extend({"type": "course-change", "categories": ["cancelled"],
                            "before": item, "after": None, "event": item} for item in old)
            changes.extend({"type": "course-change", "categories": ["added"],
                            "before": None, "after": item, "event": item} for item in new)
    for before, after in pairs:
        left, right = course_fields(before), course_fields(after)
        categories = []
        if any(left[key] != right[key] for key in ("starts_at", "ends_at")):
            categories.append("time")
        categories.extend(key for key in ("location", "teacher", "title") if left[key] != right[key])
        if categories:
            changes.append({"type": "course-change", "categories": categories,
                            "before": before, "after": after, "event": after})
    return changes


class SyncChangeTracker:
    def __init__(self, db: Database, user_id: int):
        self.db, self.user_id = db, user_id

    def record(self, source: str, events: list[dict], complete: bool) -> None:
        if source not in {"byxt", "spoc", "judge"} or not complete:
            return  # Never infer cancellations or establish a baseline from partial data.
        expected = "course" if source == "byxt" else "assignment"
        current = {str(event["external_id"]): event for event in events if event.get("kind") == expected}
        previous = self.db.get_sync_snapshot(self.user_id, source)
        changes = []
        if previous is not None:
            if source == "byxt":
                for key, stored in previous.items():
                    local = self.db.event_by_key(self.user_id, source, key)
                    if local:
                        stored["ignored"] = local.get("ignored", False)
                changes = compare_courses(previous, current)
                for change in changes:
                    before, after = change.get("before"), change.get("after")
                    if before and after and before.get("ignored"):
                        after["ignored"] = True
                        self.db.set_event_ignored(self.user_id, after["id"], True)
            else:
                for key, event in current.items():
                    old = previous.get(key)
                    if old is None:
                        changes.append({"type": "new-assignment", "categories": [],
                                        "before": None, "after": event, "event": event})
                    elif source == "spoc":
                        before, after = old.get("metadata") or {}, event.get("metadata") or {}
                        # Unknown/error and legacy max-score fields never count as grading.
                        if before.get("grading_known") and after.get("grading_known") and after.get("graded"):
                            fields = ("earned_score", "grade_comment", "graded_at")
                            if not before.get("graded") or any(before.get(f) != after.get(f) for f in fields):
                                changes.append({"type": "assignment-grade", "categories": [],
                                                "before": old, "after": event, "event": event})
                # Remember historical assignment ids even if the current-term query drops them.
                current = {**previous, **current}
        self.db.commit_sync_snapshot(self.user_id, source, current, changes,
                                     datetime.now(UTC).isoformat())


def change_matches(rule: dict, change: dict, event: dict) -> bool:
    if event.get("ignored"):
        return False
    profile = rule.get("profile")
    if profile == "course-change":
        from app.services.event_state import expired
        # No retrospective noise for lessons already over in both versions.
        versions = [value for value in (change.get("before"), change.get("after")) if value]
        if versions and all(expired(value) for value in versions):
            return False
        return change["type"] == "course-change" and bool(set(change["categories"]) &
            set((rule.get("profile_settings") or {}).get("change_types", [])))
    if profile in {"spoc-new-assignment", "judge-new-assignment"}:
        if change["type"] != "new-assignment":
            return False
        from app.services.event_state import reminder_matches
        return reminder_matches(rule, event)
    return profile == "spoc-assignment-grade" and change["type"] == "assignment-grade"
