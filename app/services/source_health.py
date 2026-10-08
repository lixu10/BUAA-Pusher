from __future__ import annotations

from typing import Any


BAD_STATUSES = {"login_required", "error", "degraded"}


def source_transition_message(
    previous: dict[str, Any] | None, current: dict[str, Any]
) -> tuple[str, str] | None:
    """Return one user-facing message only when source health meaningfully changes."""

    if previous is None or not current.get("enabled", True):
        return None
    before, after = previous.get("status"), current.get("status")
    if before == after:
        return None
    label = str(current.get("label") or current.get("id") or "数据源")
    if after in BAD_STATUSES:
        detail = str(current.get("detail") or "连接异常")
        title = {
            "login_required": f"{label}需要重新登录",
            "degraded": f"{label}部分同步失败",
            "error": f"{label}同步失败",
        }.get(str(after), f"{label}同步异常")
        return title, detail
    if before in BAD_STATUSES and after == "healthy":
        return f"{label}已恢复", "数据同步已恢复正常"
    return None
