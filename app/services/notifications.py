from __future__ import annotations

import asyncio
import json
import smtplib
import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.db import Database, now_iso
from app.features import event_available, rule_available
from app.security import CredentialVault
from app.services.rules import RuleEngine
from app.services.academic import academic_window, in_academic_window
from app.services.event_state import public_event, reminder_matches
from app.services.sync_changes import CHANGE_LABELS, change_matches, course_fields


CHANNEL_REQUIREMENTS = {
    "email": {"host", "port", "username", "password", "from", "to"},
    "sms_webhook": {"url"},
    "wxpusher": {"app_token", "uid"},
    "telegram": {"bot_token", "chat_id"},
}


def validate_channel_config(channel_type: str, config: dict[str, Any]) -> None:
    missing = [key for key in CHANNEL_REQUIREMENTS.get(channel_type, set()) if not config.get(key)]
    if missing:
        raise ValueError(f"缺少配置：{', '.join(sorted(missing))}")
    if channel_type == "sms_webhook" and not str(config["url"]).startswith("https://"):
        raise ValueError("短信网关必须使用 HTTPS")


class NotificationSender:
    def __init__(self, db: Database, vault: CredentialVault):
        self.db = db
        self.vault = vault

    async def send(self, user_id: int, channel_type: str, channel_id: int | None, title: str, body: str, trigger_id: int | None = None) -> int:
        status, error = "sent", ""
        try:
            if channel_type == "in_app":
                pass
            else:
                if channel_id is None:
                    raise ValueError("通知动作缺少渠道")
                channel = self.db.get_channel(user_id, channel_id, include_secret=True)
                if not channel or not channel["enabled"]:
                    raise ValueError("通知渠道不可用")
                if channel["type"] != channel_type:
                    raise ValueError("通知渠道类型不匹配")
                config = json.loads(self.vault.decrypt(user_id, channel["config_ciphertext"]))
                validate_channel_config(channel_type, config)
                await self._send_external(channel_type, config, title, body)
                self.db.set_channel_status(user_id, channel_id, "healthy")
        except Exception as exc:
            status, error = "failed", str(exc)[:300]
            if channel_id:
                self.db.set_channel_status(user_id, channel_id, "error", error)
        delivery_id = self.db.create_delivery({
            "user_id": user_id, "trigger_id": trigger_id, "channel": channel_type,
            "channel_id": channel_id, "title": title, "body": body,
            "status": status, "error": error, "sent_at": now_iso() if status == "sent" else None,
        })
        if channel_type == "in_app" and status == "sent":
            self.db.create_inbox(user_id, delivery_id, title, body)
        if status == "failed":
            raise RuntimeError(error)
        return delivery_id

    async def _send_external(self, channel_type: str, config: dict[str, Any], title: str, body: str) -> None:
        if channel_type == "email":
            await asyncio.to_thread(self._send_email, config, title, body)
            return
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            if channel_type == "sms_webhook":
                response = await client.post(config["url"], headers=config.get("headers") or {}, json={
                    "title": title, "message": body, "to": config.get("to")
                })
            elif channel_type == "wxpusher":
                response = await client.post("https://wxpusher.zjiecode.com/api/send/message", json={
                    "appToken": config["app_token"], "content": f"{title}\n{body}",
                    "contentType": 1, "uids": [config["uid"]],
                })
            elif channel_type == "telegram":
                response = await client.post(
                    f"https://api.telegram.org/bot{config['bot_token']}/sendMessage",
                    json={"chat_id": config["chat_id"], "text": f"{title}\n{body}"},
                )
            else:
                raise ValueError("不支持的通知渠道")
            response.raise_for_status()

    @staticmethod
    def _send_email(config: dict[str, Any], title: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"], message["From"], message["To"] = title, config["from"], config["to"]
        message.set_content(body)
        host, port = config["host"], int(config["port"])
        if config.get("ssl", port == 465):
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context()) as server:
                server.login(config["username"], config["password"])
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                if config.get("starttls", True):
                    server.starttls(context=ssl.create_default_context())
                server.login(config["username"], config["password"])
                server.send_message(message)


class TriggerDispatcher:
    def __init__(self, db: Database, sender: NotificationSender, enforce_feature_scope: bool = False):
        self.db, self.sender = db, sender
        self.enforce_feature_scope = enforce_feature_scope

    async def dispatch_due(self) -> int:
        processed = 0
        for trigger in self.db.due_triggers():
            context = self.db.trigger_context(trigger["id"])
            if not context:
                continue
            event = {
                "title": context["title"], "kind": context["kind"], "source": context["source"],
                "starts_at": context["starts_at"], "ends_at": context["ends_at"],
                "due_at": context["due_at"], "status": context["event_status"],
                "location": context["location"], "metadata": context["metadata"],
                "ignored": context.get("ignored", False),
            }
            definition = context["definition"]
            change = context.get("change")
            matches = change_matches(definition, change, event) if change else reminder_matches(definition, event)
            if not matches or (self.enforce_feature_scope and not in_academic_window(
                event, academic_window(self.db.preferences(trigger["user_id"]))
            )):
                self.db.finish_trigger(trigger["id"], "cancelled", "已忽略" if event.get("ignored") else "状态筛选不匹配或已过期")
                processed += 1
                continue
            if self.enforce_feature_scope and (
                not event_available(event) or not rule_available(definition)
                or not (self.db.get_source(trigger["user_id"], event["source"]) or {}).get("enabled", True)
            ):
                self.db.finish_trigger(trigger["id"], "cancelled", "功能未启用 / Coming Soon")
                processed += 1
                continue
            cancel_conditions = definition.get("cancel_conditions") or []
            if not context["rule_enabled"] or (
                cancel_conditions and RuleEngine.conditions_match(cancel_conditions, event)
            ):
                self.db.finish_trigger(trigger["id"], "cancelled", "取消条件已满足")
                processed += 1
                continue
            if not RuleEngine.conditions_match(definition.get("conditions", []), event):
                self.db.finish_trigger(trigger["id"], "skipped", "触发条件不成立")
                processed += 1
                continue
            quiet = self._quiet_decision(trigger["user_id"], definition.get("quiet_hours_policy", "delay"))
            # Deadline alerts must never arrive at/after the deadline, including
            # delayed quiet-hours messages and retry attempts.
            if not change and event.get("kind") == "assignment":
                deadline = RuleEngine.parse_time(event.get("due_at"))
                intended = quiet.astimezone(UTC) if isinstance(quiet, datetime) else datetime.now(UTC)
                if deadline and intended >= deadline:
                    self.db.finish_trigger(trigger["id"], "skipped", "截止前已无可发送时机")
                    processed += 1
                    continue
            if quiet == "skip":
                self.db.finish_trigger(trigger["id"], "skipped", "处于安静时段")
                processed += 1
                continue
            if isinstance(quiet, datetime):
                self.db.reschedule_trigger(trigger["id"], quiet.astimezone(UTC).isoformat(), "延后至安静时段结束")
                processed += 1
                continue
            try:
                action = definition["actions"][trigger["action_index"]]
                body = self._message_body(context, event)
                await self.sender.send(
                    trigger["user_id"], action["channel"], action.get("channel_id"),
                    context["title"], body, trigger["id"],
                )
                self.db.finish_trigger(trigger["id"], "sent")
            except Exception as exc:
                attempts = int(trigger.get("attempts") or 0)
                if attempts < 3:
                    delay = (1, 5, 15)[attempts]
                    retry_at = datetime.now(UTC) + timedelta(minutes=delay)
                    self.db.reschedule_trigger(
                        trigger["id"], retry_at.isoformat(),
                        f"发送失败，{delay} 分钟后重试：{str(exc)[:180]}",
                        increment_attempts=True,
                    )
                else:
                    self.db.finish_trigger(trigger["id"], "failed", f"重试已用尽：{str(exc)}")
            processed += 1
        return processed

    @staticmethod
    def _message_body(context: dict[str, Any], event: dict[str, Any]) -> str:
        change = context.get("change")
        if change:
            if change["type"] == "course-change":
                left = course_fields(change.get("before") or {})
                right = course_fields(change.get("after") or {})
                lines = ["、".join(CHANGE_LABELS[key] for key in change["categories"])]
                for key in change["categories"]:
                    if key == "time":
                        lines.append(f"原时间：{left['starts_at']} – {left['ends_at']}\n现时间：{right['starts_at']} – {right['ends_at']}")
                    elif key in {"location", "teacher", "title"}:
                        lines.append(f"{CHANGE_LABELS[key]}：{left[key] or '未提供'} → {right[key] or '未提供'}")
                if not any(key in change["categories"] for key in ("time",)):
                    lines.append(str(event.get("starts_at") or ""))
                lines.append(context["rule_name"])
                return "\n".join(lines)
            if change["type"] == "assignment-grade":
                meta = (change.get("after") or {}).get("metadata") or {}
                old = (change.get("before") or {}).get("metadata") or {}
                score = meta.get("earned_score")
                parts = ["作业新评分" if not old.get("graded") else "作业评分更新"]
                if score is not None:
                    parts.append(f"得分：{score}" + (f" / {meta['max_score']}" if meta.get("max_score") is not None else ""))
                if meta.get("grade_comment"):
                    parts.append(f"评语：{meta['grade_comment']}")
                if meta.get("graded_at"):
                    parts.append(f"批阅时间：{meta['graded_at']}")
                return "\n".join([*parts, context["rule_name"]])
            return "新作业发布\n" + (f"截止：{event['due_at']}\n" if event.get("due_at") else "") + context["rule_name"]
        metadata = event.get("metadata") or {}
        parts: list[str] = []
        if event.get("kind") == "grade" and metadata.get("score") is not None:
            parts.append(f"成绩 {metadata['score']}")
        elif event.get("kind") == "course":
            parts.append("课程提醒")
            if metadata.get("teacher"):
                parts.append(str(metadata["teacher"]))
        elif event.get("kind") == "signin":
            parts.append("已签到" if event.get("status") == "done" else "尚未签到")
        elif event.get("kind") == "assignment":
            parts.append(public_event(event)["status_label"])
            if metadata.get("total_problems"):
                parts.append(f"提交进度 {metadata.get('submitted_count', 0)}/{metadata['total_problems']}")
        elif event.get("kind") == "fitness":
            current, target = metadata.get("current"), metadata.get("target")
            if current is not None and target is not None:
                parts.append(f"学期进度 {current}/{target}")
            weekly_max = metadata.get("week_target")
            if metadata.get("completion_impossible"):
                parts.append("按剩余每周上限已无法达标")
            elif metadata.get("must_max_every_week") and weekly_max:
                parts.append(f"从本周起每周需完成 {weekly_max} 次")
        if context.get("location"):
            parts.append(str(context["location"]))
        time_text = context.get("due_at") or context.get("starts_at") or context.get("ends_at")
        if time_text:
            parts.append(str(time_text))
        parts.append(str(context["rule_name"]))
        return " · ".join(parts)

    def _quiet_decision(self, user_id: int, policy: str) -> str | datetime | None:
        if policy == "ignore":
            return None
        preferences = self.db.preferences(user_id)
        start, end = preferences.get("quiet_start"), preferences.get("quiet_end")
        if not start or not end:
            return None
        now = datetime.now(ZoneInfo(preferences.get("timezone") or "Asia/Shanghai"))
        start_hour, start_minute = map(int, start.split(":"))
        end_hour, end_minute = map(int, end.split(":"))
        start_at = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
        end_at = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
        if start_at <= end_at:
            active = start_at <= now < end_at
        else:
            active = now >= start_at or now < end_at
            if now >= start_at:
                end_at += timedelta(days=1)
        if not active:
            return None
        return "skip" if policy == "skip" else end_at
