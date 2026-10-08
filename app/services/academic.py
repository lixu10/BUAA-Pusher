"""Per-user overlapping academic windows, calculated in China time."""
from datetime import datetime, timezone, timedelta
from typing import Any

CHINA = timezone(timedelta(hours=8))


def academic_window(preferences: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    today = (now or datetime.now(CHINA)).astimezone(CHINA).date()
    windows = []
    for year in (today.year - 1, today.year):
        for start, end in [(f"{year}-02-01", f"{year}-09-01"),
                           (f"{year}-08-01", f"{year + 1}-03-01")]:
            if start <= today.isoformat() < end:
                windows.append((start, end))
    custom_start = preferences.get("academic_start_date")
    custom_end = preferences.get("academic_end_date")
    return {"enabled": preferences.get("academic_filter_enabled", True),
            "mode": "custom" if custom_start else "auto",
            "start": custom_start or min(start for start, _ in windows),
            "end": custom_end if custom_start else max(end for _, end in windows)}


def in_academic_window(event: dict[str, Any], window: dict[str, Any]) -> bool:
    if not window["enabled"]:
        return True
    raw = event.get("due_at") or event.get("starts_at") or (event.get("metadata") or {}).get("opens_at")
    if not raw:
        return True  # A missing deadline must stay visible as unknown, not be lost.
    try:
        timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=CHINA)
        day = timestamp.astimezone(CHINA).date().isoformat()
        return day >= window["start"] and (not window.get("end") or day < window["end"])
    except (ValueError, TypeError):
        return True
