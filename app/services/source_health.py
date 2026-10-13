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
        mode = "学校 WebVPN" if current.get("network_mode") == "webvpn" else "直连"
        guidance = "在连接页面完成学校认证；需要验证码时手动登录" if after == "login_required" else "检查网络和访问模式，可在连接页面单独重试同步"
        lines = [f"来源：{label}", f"访问方式：{mode}", f"原因：{detail}", "数据：上次缓存已保留"]
        if current.get("last_sync_at"):
            from app.services.rules import RuleEngine
            from zoneinfo import ZoneInfo
            time = RuleEngine.parse_time(current["last_sync_at"])
            if time:
                lines.append("本次同步：" + time.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M"))
        lines.append(f"建议：{guidance}")
        return title, "\n".join(lines)
    if before in BAD_STATUSES and after == "healthy":
        return f"{label}已恢复", f"来源：{label}\n状态：数据同步已恢复正常\n同步事项：{current.get('event_count', 0)} 项\n后续提醒将按最新状态重新判断"
    return None
