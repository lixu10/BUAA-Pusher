"""Read-only Boya catalog, enrollment states, filters and durable reminder plans."""
from datetime import UTC, datetime, timedelta

from app.services.rules import RuleEngine


CATALOG_PROFILES = {"boya-new-course", "boya-seats", "boya-enrollment-open", "boya-enrollment-soon", "boya-enrollment-closing"}
TIMED_PROFILES = {"boya-enrollment-open", "boya-enrollment-soon", "boya-enrollment-closing"}
STATE_LABELS = {"preview": "未开放", "available": "可报名", "full": "已满", "closed": "报名结束", "started": "已开课", "ended": "已结束", "selected": "已报名", "unknown": "状态未知"}


def enrollment_state(event, now=None):
    now = now or datetime.now(UTC)
    meta = event.get("metadata") or {}
    start, end = RuleEngine.parse_time(event.get("starts_at")), RuleEngine.parse_time(event.get("ends_at"))
    opens, closes = (RuleEngine.parse_time(meta.get(k)) for k in ("enrollment_starts_at", "enrollment_ends_at"))
    if meta.get("selected") is True:
        return "selected"
    if end and now >= end:
        return "ended"
    if start and now >= start:
        return "started"
    if closes and now >= closes:
        return "closed"
    if opens and now < opens:
        return "preview"
    if not opens or not closes or not start:
        return "unknown"
    remaining = meta.get("remaining")
    if remaining is None:
        return "unknown"
    return "available" if remaining > 0 else "full"


def filter_matches(settings, event):
    meta = event.get("metadata") or {}
    if settings.get("course_ids") and str(meta.get("course_id")) not in settings["course_ids"]:
        return False
    if settings.get("categories") and not set(settings["categories"]) & set(meta.get("category_ids") or []):
        return False
    if settings.get("campuses") and not meta.get("all_campuses") and not set(settings["campuses"]) & set(meta.get("campus_ids") or []):
        return False
    if settings.get("sign_types") and str(meta.get("sign_type")) not in settings["sign_types"]:
        return False
    title = str(event.get("title", "")).casefold()
    if settings.get("keywords") and not any(word.casefold() in title for word in settings["keywords"]):
        return False
    return not any(word.casefold() in title for word in settings.get("exclude_keywords", []))


def catalog_matches(rule, event, change=None, now=None):
    settings, profile = rule.get("profile_settings") or {}, rule.get("profile")
    meta = event.get("metadata") or {}
    if event.get("ignored") or meta.get("selected") is not False or not filter_matches(settings, event):
        return False
    state = enrollment_state(event, now)
    if profile == "boya-new-course":
        return bool(change and change["type"] == "boya-new-course" and
                    (state == "preview" and settings.get("include_preview", True) or state == "available" and (meta.get("remaining") or 0) >= settings.get("min_remaining", 1)))
    if profile == "boya-seats":
        previous = ((change or {}).get("before") or {}).get("metadata") or {}
        minimum = settings.get("min_remaining", 1)
        return bool(change and change["type"] == "boya-seats" and state == "available" and
                    previous.get("remaining") is not None and previous["remaining"] < minimum <= (meta.get("remaining") or 0))
    if profile in TIMED_PROFILES:
        if state not in {"preview", "available"}:
            return False
        if state == "available" and (meta.get("remaining") or 0) < settings.get("min_remaining", 1):
            return False
        if profile in {"boya-enrollment-open", "boya-enrollment-closing"}:
            return state == "available"
        return state == "preview"
    return False


def record_catalog(db, user_id, courses):
    previous = db.get_sync_snapshot(user_id, "bykc_catalog")
    current = {event["external_id"]: event for event in courses}
    changes = []
    if previous is not None:
        for key, event in current.items():
            before = previous.get(key)
            if before is None:
                changes.append({"type": "boya-new-course", "categories": [], "before": None, "after": event, "event": event})
            else:
                old, new = before["metadata"].get("remaining"), event["metadata"].get("remaining")
                if old is not None and new is not None and new > old:
                    changes.append({"type": "boya-seats", "categories": [], "before": before, "after": event, "event": event})
    db.commit_sync_snapshot(user_id, "bykc_catalog", current, changes, datetime.now(UTC).isoformat(), rule_source="bykc")


def plan_catalog(db, user_id, rule, now, window=None):
    from app.services.academic import in_academic_window
    profile, settings = rule["profile"], rule.get("profile_settings") or {}
    field = "enrollment_ends_at" if profile == "boya-enrollment-closing" else "enrollment_starts_at"
    minutes = [0] if profile == "boya-enrollment-open" else settings.get("advance_minutes", [10, 1])
    desired = set()
    inserted = 0
    existing = db.queued_catalog_keys(user_id, rule["id"])
    for event in db.list_boya_courses(user_id):
        meta = event["metadata"]
        if not filter_matches(settings, event) or meta.get("selected") is not False or (window and not in_academic_window(event, window)):
            continue
        state = enrollment_state(event, now)
        if state not in {"preview", "available"}:
            continue
        anchor = RuleEngine.parse_time(meta.get(field))
        if not anchor:
            continue
        for minute in minutes:
            at = anchor - timedelta(minutes=minute)
            for action_index, _ in enumerate(rule["actions"]):
                key = f"catalog:{user_id}:{rule['id']}:{rule['updated_at']}:{meta['course_id']}:{at.isoformat()}:{action_index}"
                if at < now - timedelta(seconds=60) and key not in existing:
                    continue
                desired.add(key)
                payload = {"type": "boya-window", "categories": [], "before": None, "after": event, "event": event,
                           "anchor_field": field, "anchor_time": meta[field], "advance_minutes": minute}
                inserted += db.queue_catalog_trigger(user_id, rule["id"], payload, action_index, at.isoformat(), key)
    db.cancel_catalog_plans(user_id, rule["id"], desired)
    return inserted


def catalog_options(events):
    categories, campuses = {}, {}
    for event in events:
        meta = event["metadata"]
        categories.update(zip(meta.get("category_ids") or [], meta.get("category_names") or []))
        campuses.update(zip(meta.get("campus_ids") or [], meta.get("campus_names") or []))
    return {"categories": [{"value": key, "label": value} for key, value in sorted(categories.items(), key=lambda item: item[1])],
            "campuses": [{"value": key, "label": value} for key, value in sorted(campuses.items(), key=lambda item: item[1])],
            "sign_types": [{"value": "1", "label": "常规签到"}, {"value": "2", "label": "自主签到"}]}
