"""Current product scope; parked features keep their code and stored data."""
from typing import Any

SCHOOL_SOURCES = frozenset({"spoc", "byxt", "judge", "iclass", "grade", "bykc", "cgyy", "ygdk", "libbook"})
ACTIVE_SOURCES = frozenset({"spoc", "byxt", "judge", "iclass", "bykc"})
ACTIVE_KINDS = frozenset({"assignment", "course", "boya"})
CATALOG_TIMED_PROFILES = frozenset({"boya-enrollment-open", "boya-enrollment-soon", "boya-enrollment-closing"})
CATALOG_PROFILES = CATALOG_TIMED_PROFILES | {"boya-new-course", "boya-seats"}
CHANGE_PROFILES = {"course-change": ("byxt", "course"),
                   "spoc-new-assignment": ("spoc", "assignment"),
                   "judge-new-assignment": ("judge", "assignment"),
                   "spoc-assignment-grade": ("spoc", "assignment"),
                   "boya-course-change": ("bykc", "boya"), "boya-new-course": ("bykc", "boya_catalog"), "boya-seats": ("bykc", "boya_catalog")}
ACTIVE_PROFILES = frozenset({"assignment", "course", "signin", "spoc-assignment", "judge-assignment", "boya-course", *CATALOG_PROFILES, *CHANGE_PROFILES})


def event_available(event: dict[str, Any]) -> bool:
    if event.get("source") == "custom":
        return event.get("kind") == "custom"
    if event.get("source") == "bykc":
        return event.get("kind") in {"boya", "boya_catalog"}
    expected = "course" if event.get("source") == "byxt" else "assignment"
    return event.get("source") in ACTIVE_SOURCES and event.get("kind") == expected


def rule_available(rule: dict[str, Any]) -> bool:
    scope = rule.get("scope") or {}
    kinds, sources = set(scope.get("kinds") or []), set(scope.get("sources") or [])
    profile = rule.get("profile")
    if profile == "custom-event":
        event_ids = scope.get("event_ids") or []
        return sources == {"custom"} and kinds == {"custom"} and len(event_ids) == 1 and type(event_ids[0]) is int and event_ids[0] > 0
    if "custom" in sources or "custom" in kinds:
        return False
    if profile == "signin":
        return sources == {"iclass"} and kinds == {"signin"} and bool((rule.get("profile_settings") or {}).get("check_times"))
    if "iclass" in sources or "signin" in kinds:
        return False  # Old polling/escalation rules require explicit reconfiguration.
    scopes = {**CHANGE_PROFILES, "course": ("byxt", "course"),
              "spoc-assignment": ("spoc", "assignment"), "judge-assignment": ("judge", "assignment"),
              "boya-course": ("bykc", "boya"), **{key: ("bykc", "boya_catalog") for key in CATALOG_TIMED_PROFILES}}
    if profile in scopes:
        source, kind = scopes[profile]
        return sources == {source} and kinds == {kind}
    return bool(kinds) and kinds <= ACTIVE_KINDS and sources <= ACTIVE_SOURCES and (
        profile is None or profile in ACTIVE_PROFILES
    )


def public_rule(rule: dict[str, Any]) -> dict[str, Any]:
    available = rule_available(rule)
    scope = rule.get("scope") or {}
    legacy_signin = "iclass" in scope.get("sources", []) and "signin" in scope.get("kinds", [])
    return {**rule, "available": available, "enabled": bool(rule.get("enabled")) and available,
            "availability": "available" if available else "needs_configuration" if legacy_signin else "coming_soon"}
