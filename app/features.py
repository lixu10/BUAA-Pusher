"""Current product scope; parked features keep their code and stored data."""
from typing import Any

SCHOOL_SOURCES = frozenset({"spoc", "byxt", "judge", "iclass", "grade", "bykc", "cgyy", "ygdk", "libbook"})
ACTIVE_SOURCES = frozenset({"spoc", "byxt", "judge", "iclass"})
ACTIVE_KINDS = frozenset({"assignment", "course"})
CHANGE_PROFILES = {"course-change": ("byxt", "course"),
                   "spoc-new-assignment": ("spoc", "assignment"),
                   "judge-new-assignment": ("judge", "assignment"),
                   "spoc-assignment-grade": ("spoc", "assignment")}
ACTIVE_PROFILES = frozenset({"assignment", "course", "signin", "spoc-assignment", "judge-assignment", *CHANGE_PROFILES})


def event_available(event: dict[str, Any]) -> bool:
    expected = "course" if event.get("source") == "byxt" else "assignment"
    return event.get("source") in ACTIVE_SOURCES and event.get("kind") == expected


def rule_available(rule: dict[str, Any]) -> bool:
    scope = rule.get("scope") or {}
    kinds, sources = set(scope.get("kinds") or []), set(scope.get("sources") or [])
    profile = rule.get("profile")
    if profile == "signin":
        return sources == {"iclass"} and kinds == {"signin"} and bool((rule.get("profile_settings") or {}).get("check_times"))
    if "iclass" in sources or "signin" in kinds:
        return False  # Old polling/escalation rules require explicit reconfiguration.
    scopes = {**CHANGE_PROFILES, "course": ("byxt", "course"),
              "spoc-assignment": ("spoc", "assignment"), "judge-assignment": ("judge", "assignment")}
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
