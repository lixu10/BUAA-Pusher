from __future__ import annotations

from typing import Any, Literal
from datetime import date

import re

from pydantic import BaseModel, Field, field_validator, model_validator

from app.features import CHANGE_PROFILES


class RegisterRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    display_name: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=10, max_length=200)

    @field_validator("password")
    @classmethod
    def password_strength(cls, value: str) -> str:
        if value.isalpha() or value.isdigit():
            raise ValueError("密码需同时包含字母和数字或符号")
        return value

    @field_validator("email")
    @classmethod
    def email_shape(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("邮箱格式不正确")
        return value


class UserLoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class SchoolLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)
    captcha: str | None = Field(default=None, max_length=20)
    remember_password: bool = False
    network_mode: Literal["direct", "webvpn"] = "direct"


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    kind: str = Field(default="custom", max_length=40)
    starts_at: str | None = None
    ends_at: str | None = None
    due_at: str | None = None
    all_day: bool = False
    location: str | None = Field(default=None, max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceUpdate(BaseModel):
    enabled: bool | None = None
    refresh_interval_minutes: int | None = Field(default=None, ge=0, le=1440)
    network_mode: Literal["direct", "webvpn"] | None = None


class EventIgnoreUpdate(BaseModel):
    ignored: bool


class RuleUpdate(BaseModel):
    enabled: bool | None = None
    offsets: list[int] | None = None
    channels: list[str] | None = None


class RuleScope(BaseModel):
    kinds: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class RuleTrigger(BaseModel):
    anchor: str = Field(pattern="^(starts_at|ends_at|due_at)$")
    offset_minutes: int = Field(ge=-525600, le=525600)


class RuleCondition(BaseModel):
    field: str = Field(min_length=1, max_length=80)
    op: str = Field(
        pattern="^(eq|ne|in|not_in|exists|not_exists|gt|gte|lt|lte|contains|not_contains)$"
    )
    value: Any = None


class RuleRepeat(BaseModel):
    every_minutes: int | None = Field(default=None, ge=1, le=10080)
    max_count: int = Field(default=1, ge=1, le=60)
    until_anchor: str | None = Field(default=None, pattern="^(starts_at|ends_at|due_at)$")


class RuleAction(BaseModel):
    channel: str = Field(min_length=1, max_length=40)
    channel_id: int | None = None
    after_minutes: int = Field(default=0, ge=0, le=10080)


class AutomationRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    scope: RuleScope
    trigger: RuleTrigger | None = None
    triggers: list[RuleTrigger] = Field(default_factory=list, max_length=10)
    conditions: list[RuleCondition] = Field(default_factory=list, max_length=10)
    cancel_conditions: list[RuleCondition] = Field(default_factory=list, max_length=10)
    repeat: RuleRepeat = Field(default_factory=RuleRepeat)
    actions: list[RuleAction] = Field(min_length=1, max_length=10)
    quiet_hours_policy: str = Field(default="delay", pattern="^(delay|skip|ignore)$")
    profile: str | None = Field(default=None, max_length=40)
    profile_settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def requires_trigger(self):
        if self.profile == "course" and "advance_minutes" in self.profile_settings:
            timing = self.profile_settings.get("timing", "before_start")
            minutes = self.profile_settings["advance_minutes"]
            if timing not in {"before_start", "before_end"} or type(minutes) is not int or not 0 <= minutes <= 10080:
                raise ValueError("选择上课前或课程结束前，提前分钟数为 0–10080 的整数")
            self.trigger = RuleTrigger(anchor="ends_at" if timing == "before_end" else "starts_at", offset_minutes=-minutes)
            self.triggers = [self.trigger]
            self.repeat = RuleRepeat()
        if self.profile in CHANGE_PROFILES:
            self.trigger = None
            self.triggers = []
            self.repeat = RuleRepeat()
            if self.profile == "course-change":
                categories = self.profile_settings.get("change_types", [])
                if not isinstance(categories, list) or not categories or any(
                    not isinstance(value, str) or value not in {"time", "location", "teacher", "title", "added", "cancelled"}
                    for value in categories):
                    raise ValueError("至少选择一种课程变更类别")
            return self
        if self.trigger is None and not self.triggers and not (
            self.profile in {"assignment", "spoc-assignment", "judge-assignment"}
            and isinstance(self.profile_settings.get("reminder_times"), list)
        ):
            raise ValueError("至少设置一个触发时机")
        if self.profile in {"assignment", "spoc-assignment", "judge-assignment"}:
            points = self.profile_settings.get("reminder_times")
            if points is not None:
                if not isinstance(points, list) or not 1 <= len(points) <= 10:
                    raise ValueError("设置 1–10 个提醒时间点")
                offsets = []
                for point in points:
                    if not isinstance(point, dict):
                        raise ValueError("提醒时间格式错误")
                    values = [point.get(key, 0) for key in ("days", "hours", "minutes")]
                    if any(type(value) is not int or value < 0 for value in values):
                        raise ValueError("天、小时、分钟必须是非负整数")
                    days, hours, minutes = values
                    if days > 365 or hours > 23 or minutes > 59:
                        raise ValueError("小时为 0–23，分钟为 0–59，最多提前 365 天")
                    total = days * 1440 + hours * 60 + minutes
                    if not 1 <= total <= 525600:
                        raise ValueError("必须至少提前 1 分钟，不能在截止时提醒")
                    offsets.append(total)
                if len(set(offsets)) != len(offsets):
                    raise ValueError("提醒时间点不能重复")
                if any(action.after_minutes for action in self.actions):
                    raise ValueError("请直接添加提醒时间点，不使用延后通知")
                self.triggers = [RuleTrigger(anchor="due_at", offset_minutes=-total) for total in offsets]
                self.trigger = self.triggers[0]
                self.repeat = RuleRepeat()
            elif any(t.anchor != "due_at" or t.offset_minutes >= 0
                     for t in (self.triggers or [self.trigger])):
                raise ValueError("作业提醒必须在截止前，至少提前 1 分钟")
        return self


class AutomationRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None
    scope: RuleScope | None = None
    trigger: RuleTrigger | None = None
    triggers: list[RuleTrigger] | None = Field(default=None, max_length=10)
    conditions: list[RuleCondition] | None = Field(default=None, max_length=10)
    cancel_conditions: list[RuleCondition] | None = Field(default=None, max_length=10)
    repeat: RuleRepeat | None = None
    actions: list[RuleAction] | None = Field(default=None, min_length=1, max_length=10)
    quiet_hours_policy: str | None = Field(default=None, pattern="^(delay|skip|ignore)$")
    profile: str | None = Field(default=None, max_length=40)
    profile_settings: dict[str, Any] | None = None


class NotificationChannelCreate(BaseModel):
    type: str = Field(pattern="^(email|sms_webhook|wxpusher|telegram)$")
    name: str = Field(min_length=1, max_length=80)
    config: dict[str, Any]
    enabled: bool = True


class NotificationChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class PreferencesUpdate(BaseModel):
    timezone: str = Field(default="Asia/Shanghai", max_length=80)
    quiet_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    quiet_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    academic_filter_enabled: bool = True
    academic_start_date: date | None = None
    academic_end_date: date | None = None

    @model_validator(mode="after")
    def academic_dates(self):
        if self.academic_end_date and not self.academic_start_date:
            raise ValueError("设置结束日期时需要同时设置开始日期")
        if self.academic_end_date and self.academic_end_date <= self.academic_start_date:
            raise ValueError("结束日期必须晚于开始日期")
        return self
