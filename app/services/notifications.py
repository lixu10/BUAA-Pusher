from __future__ import annotations

import asyncio
import json
import smtplib
import ssl
import time
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
        if channel_type == "sms_webhook":
            import re
            entry = next((line for line in body.splitlines() if line.startswith("学校入口：")), "")
            body = re.split(r"\n(?:正文摘要|简介|教师评语)：", body, maxsplit=1)[0]
            body = body[:900] + (f"\n{entry}" if entry and entry not in body[:900] else "")
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
    def __init__(self, db: Database, sender: NotificationSender, enforce_feature_scope: bool = False, attendance_checker=None, source_refresher=None):
        self.db, self.sender = db, sender
        self.enforce_feature_scope = enforce_feature_scope
        self.attendance_checker = attendance_checker
        self.source_refresher = source_refresher
        self.catalog_refresher = None
        self.active_groups: set[tuple[int, str]] = set()
        self.refresh_slots = asyncio.Semaphore(4)

    async def dispatch_due(self) -> int:
        groups = {}
        # Active source groups are already being processed by an earlier tick.
        # Look beyond them so they cannot hide another user's due reminders.
        for trigger in self.db.due_triggers(limit=500):
            context = self.db.trigger_context(trigger["id"])
            if not context or context.get("status") != "queued":
                continue
            key = (trigger["user_id"], "signin" if context["definition"].get("profile") == "signin" else "bykc_catalog" if context["kind"] == "boya_catalog" else context["source"])
            if key not in self.active_groups:
                groups.setdefault(key, []).append(trigger)
                if sum(map(len, groups.values())) >= 50:
                    break
        self.active_groups.update(groups)
        async def dispatch_group(key, triggers):
            try:
                return await self._dispatch_batch(triggers)
            finally:
                self.active_groups.discard(key)
        results = await asyncio.gather(*(dispatch_group(key, triggers) for key, triggers in groups.items()), return_exceptions=True)
        return sum(result for result in results if isinstance(result, int))

    async def _dispatch_batch(self, triggers) -> int:
        processed = 0
        refreshed = None
        refreshed_at = 0
        for trigger in triggers:
            context = self.db.trigger_context(trigger["id"])
            if not context or context.get("status") != "queued":
                continue
            signin = context["definition"].get("profile") == "signin"
            catalog = context["kind"] == "boya_catalog"
            if catalog and context.get("current_event") is None:
                self.db.finish_trigger(trigger["id"], "cancelled", "目录课程已撤销")
                processed += 1
                continue
            # Quiet-hour deferrals require no network work until their actual
            # delivery time. Checkpoint attendance already has its own reader.
            if context["source"] != "custom" and (self.catalog_refresher if catalog else self.source_refresher) and not signin and context["rule_enabled"] and not context.get("ignored") and (
                not self.enforce_feature_scope or (rule_available(context["definition"])
                    and (self.db.get_source(trigger["user_id"], context["source"]) or {}).get("enabled", True))
            ) and self._quiet_decision(trigger["user_id"], context["definition"].get("quiet_hours_policy", "delay")) is None:
                if refreshed is None or (refreshed[0] and time.monotonic() - refreshed_at > 30):
                    try:
                        async with self.refresh_slots:
                            if catalog:
                                ids = sorted({str(item["metadata"]["course_id"]) for row in triggers if (item := self.db.trigger_context(row["id"])) and item.get("current_event")})
                                refreshed = await self.catalog_refresher(trigger["user_id"], ids)
                            else:
                                refreshed = await self.source_refresher(trigger["user_id"], context["source"])
                    except Exception:
                        refreshed = (False, "通知前同步失败")
                    refreshed_at = time.monotonic()
                # Sync replans/removes scheduled triggers and may change rules,
                # dates, completion, ignore state or source availability.
                context = self.db.trigger_context(trigger["id"])
                if not context or context.get("status") != "queued" or RuleEngine.parse_time(context["run_at"]) > datetime.now(UTC):
                    continue
                trigger = context
                if not refreshed[0]:
                    self._defer_refresh(trigger, refreshed[1])
                    processed += 1
                    continue
                self.db.reset_trigger_refresh_attempts(trigger["id"])
                if context.get("change") and not self._change_still_current(context):
                    self.db.finish_trigger(trigger["id"], "cancelled", "变化已被更新或撤销")
                    processed += 1
                    continue
            event = {
                "id": context.get("event_id"),
                "title": context["title"], "kind": context["kind"], "source": context["source"],
                "starts_at": context["starts_at"], "ends_at": context["ends_at"],
                "due_at": context["due_at"], "status": context["event_status"],
                "location": context["location"], "metadata": context["metadata"],
                "ignored": context.get("ignored", False),
            }
            definition = context["definition"]
            if context["source"] == "custom" and not RuleEngine.matches_scope(definition["scope"], event):
                self.db.finish_trigger(trigger["id"], "cancelled", "日程与提醒不匹配")
                processed += 1
                continue
            signin = definition.get("profile") == "signin"
            change = context.get("change")
            if catalog and definition.get("profile") == "boya-seats":
                cooldown = (definition.get("profile_settings") or {}).get("cooldown_minutes", 15)
                if cooldown and self.db.recent_boya_notice(trigger["user_id"], context["rule_id"], context["external_id"], context["change_id"], (datetime.now(UTC) - timedelta(minutes=cooldown)).isoformat()):
                    self.db.finish_trigger(trigger["id"], "skipped", "名额变化通知间隔内")
                    processed += 1
                    continue
            matches = not event.get("ignored") if signin else change_matches(definition, change, event) if change else reminder_matches(definition, event)
            if not matches or (self.enforce_feature_scope and not in_academic_window(
                event, academic_window(self.db.preferences(trigger["user_id"]))
            )):
                self.db.finish_trigger(trigger["id"], "cancelled", "已忽略" if event.get("ignored") else "状态筛选不匹配或已过期")
                processed += 1
                continue
            if self.enforce_feature_scope and (
                not event_available(event) or not rule_available(definition)
                or not (self.db.get_source(trigger["user_id"], "iclass" if signin else event["source"]) or {}).get("enabled", not signin)
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
                end = RuleEngine.parse_time(event.get("ends_at"))
                if signin and (not end or quiet.astimezone(UTC) > end):
                    self.db.finish_trigger(trigger["id"], "skipped", "课程结束前无可发送时机")
                    processed += 1
                    continue
                self.db.reschedule_trigger(trigger["id"], quiet.astimezone(UTC).isoformat(), "延后至安静时段结束")
                processed += 1
                continue
            if signin:
                end = RuleEngine.parse_time(event.get("ends_at"))
                if not end or datetime.now(UTC) > end + timedelta(seconds=30) or not self.attendance_checker:
                    self.db.finish_trigger(trigger["id"], "skipped", "签到检查已过期或不可用")
                    processed += 1
                    continue
                result, detail = await self.attendance_checker(trigger["user_id"], context, event, definition)
                if result != "missing":
                    self.db.finish_trigger(trigger["id"], "cancelled" if result == "done" else "failed" if result == "error" else "skipped", detail)
                    processed += 1
                    continue
                if datetime.now(UTC) > end + timedelta(seconds=30):
                    self.db.finish_trigger(trigger["id"], "skipped", "查询完成时课程检查已过期")
                    processed += 1
                    continue
            try:
                action = definition["actions"][trigger["action_index"]]
                context["network_mode"] = (self.db.get_source(trigger["user_id"], "iclass" if signin else event["source"]) or {}).get("network_mode", "direct")
                if signin:
                    latest = self.db.trigger_context(trigger["id"])
                    if latest:
                        event["metadata"] = latest["metadata"]
                body = self._message_body(context, event, self.db.preferences(trigger["user_id"]).get("timezone", "Asia/Shanghai"))
                await self.sender.send(
                    trigger["user_id"], action["channel"], action.get("channel_id"),
                    f"{context['title']}未签到" if signin else context["title"], body, trigger["id"],
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

    def _defer_refresh(self, trigger, detail):
        attempts = int(trigger.get("refresh_attempts") or 0)
        if attempts >= 3:
            self.db.finish_trigger(trigger["id"], "failed", f"通知前同步重试已用尽：{detail}")
            return
        delay = (1, 5, 15)[attempts]
        retry_at = datetime.now(UTC) + timedelta(minutes=delay)
        # A retry never sends a deadline reminder at or beyond the deadline.
        deadline = RuleEngine.parse_time(trigger.get("due_at"))
        if not trigger.get("change") and trigger.get("kind") == "assignment" and deadline and retry_at >= deadline:
            self.db.finish_trigger(trigger["id"], "skipped", "通知前同步未成功，截止前已无重试时机")
            return
        self.db.reschedule_trigger(trigger["id"], retry_at.isoformat(),
            f"通知前同步未成功，{delay} 分钟后重试：{detail}", increment_refresh_attempts=True)

    @staticmethod
    def _change_still_current(context):
        change, current = context["change"], context.get("current_event")
        if (change.get("event") or {}).get("kind") == "boya_catalog":
            return bool(current and (change["type"] != "boya-window" or current["metadata"].get(change["anchor_field"]) == change["anchor_time"]))
        if change["type"] == "course-change":
            after = change.get("after")
            return current is None if after is None else bool(current and course_fields(current) == course_fields(after))
        if current is None:
            return False
        if change["type"] == "assignment-grade":
            after = (change.get("after") or {}).get("metadata") or {}
            latest = current.get("metadata") or {}
            return latest.get("graded") and latest.get("grading_known") and all(
                latest.get(key) == after.get(key) for key in ("earned_score", "grade_comment", "graded_at"))
        return True

    @staticmethod
    def _message_body(context: dict[str, Any], event: dict[str, Any], timezone="Asia/Shanghai") -> str:
        from app.services.notification_content import message_body
        return message_body(context, event, timezone)


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
