"""Human readable, field-whitelisted notification content."""
from datetime import UTC, datetime
from math import ceil
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.rules import RuleEngine
from app.services.event_state import public_event
from app.services.sync_changes import CHANGE_LABELS, course_fields


def message_body(context, event, timezone="Asia/Shanghai"):
    try:
        zone = ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        zone = ZoneInfo("Asia/Shanghai")
    def date(value):
        at = RuleEngine.parse_time(value)
        if not at:
            return None
        at = at.astimezone(zone)
        return at.strftime("%Y-%m-%d %H:%M:%S") + " 周" + "一二三四五六日"[at.weekday()]
    def remaining(value):
        at = RuleEngine.parse_time(value)
        if not at:
            return None
        minutes = ceil((at - datetime.now(UTC)).total_seconds() / 60)
        if minutes <= 0:
            return "已到时间"
        return " ".join(f"{n}{unit}" for n, unit in [(minutes // 1440, "天"), (minutes % 1440 // 60, "小时"), (minutes % 60, "分钟")] if n)
    lines = []
    def field(label, value):
        if value is not None and value != "":
            lines.append(f"{label}：{value}")
    def excerpt(value, limit):
        return str(value) if len(str(value)) <= limit else str(value)[:limit].rstrip() + "…（完整内容见详情）"
    meta, rule, change = event.get("metadata") or {}, context.get("definition") or {}, context.get("change")
    profile = rule.get("profile")
    labels = {"signin": "课程未签到", "boya-new-course": "新博雅课程", "boya-seats": "博雅名额回流", "boya-enrollment-open": "博雅报名已开放", "boya-enrollment-soon": "博雅报名即将开放", "boya-enrollment-closing": "博雅报名即将截止"}
    lines.append(labels.get(profile, "课程变更" if change and change["type"] == "course-change" else "作业评分更新" if change and change["type"] == "assignment-grade" else "新作业发布" if change and change["type"] == "new-assignment" else "作业截止提醒" if event["kind"] == "assignment" else "课程提醒" if event["kind"] in {"course", "boya"} else "事项提醒"))
    field("事项", event.get("title"))
    field("来源", {"byxt": "教务课表", "bykc": "博雅课程", "spoc": "SPOC", "judge": "JUDGE", "iclass": "iClass"}.get("iclass" if profile == "signin" else event.get("source"), event.get("source")))
    field("课程", meta.get("course_name"))
    teacher = meta.get("teacher")
    if isinstance(teacher, list):
        teacher = "、".join(str(value.get("name") or value.get("teacherName") or "") if isinstance(value, dict) else str(value) for value in teacher)
    elif isinstance(teacher, dict):
        teacher = teacher.get("name") or teacher.get("teacherName")
    field("教师", teacher)
    field("地点", event.get("location"))
    if event.get("starts_at"):
        field("开始", date(event["starts_at"]))
    if event.get("ends_at"):
        field("结束", date(event["ends_at"]))
    if event.get("due_at"):
        field("截止", date(event["due_at"]))
        field("距离截止", remaining(event["due_at"]))
    if profile == "signin":
        field("签到状态", "未签到")
        field("检查时间", date((meta.get("_attendance") or {}).get("checked_at")) or date(datetime.now(UTC).isoformat()))
        field("距离课程结束", remaining(event.get("ends_at")))
    elif event["kind"] == "assignment":
        field("提交状态", public_event(event)["status_label"])
        field("作业类型", f"类型 {meta['assignment_type']}" if str(meta.get("assignment_type", "")).isdigit() else meta.get("assignment_type"))
        field("开放", date(meta.get("opens_at")))
        field("提交时间", date(meta.get("submitted_at")))
        if meta.get("total_problems"):
            field("提交进度", f"{meta.get('submitted_count', '未知')} / {meta['total_problems']} 题")
        score = meta.get("earned_score") if event.get("source") == "spoc" else meta.get("earned_score", meta.get("score"))
        field("得分", f"{score} / {meta['max_score']}" if score is not None and meta.get("max_score") is not None else score)
        field("满分", meta.get("max_score"))
        field("批阅时间", date(meta.get("graded_at")))
    elif event["kind"] == "boya_catalog":
        field("分类", " / ".join(meta.get("category_names") or []))
        field("适用校区", "全部校区" if meta.get("all_campuses") else "、".join(meta.get("campus_names") or []))
        field("签到方式", {"1": "仅签到", "2": "签到＋签退"}.get(meta.get("sign_type"), "未知"))
        field("报名开始", date(meta.get("enrollment_starts_at")))
        field("距离报名开放", remaining(meta.get("enrollment_starts_at")) if profile == "boya-enrollment-soon" else None)
        field("报名截止", date(meta.get("enrollment_ends_at")))
        field("余量", meta.get("remaining"))
        field("已报名人数", f"{meta.get('current_count', '未知')} / {meta['max_count']}" if meta.get("max_count") is not None else meta.get("current_count"))
        field("本人报名", "已报名" if meta.get("selected") is True else "未报名" if meta.get("selected") is False else "未知")
    elif event["kind"] in {"course", "boya"}:
        field("课程状态", public_event(event)["status_label"])
        field("距离开始", remaining(event.get("starts_at")))
        field("课程代码", meta.get("courseCode"))
        field("教学周", meta.get("week_number"))
    if event["kind"] == "boya":
        field("分类", " / ".join(meta.get("category_names") or []))
        field("适用校区", "全部校区" if meta.get("all_campuses") else "、".join(meta.get("campus_names") or []))
        field("签到方式", {"1": "仅签到", "2": "签到＋签退"}.get(meta.get("sign_type"), "未知"))
        for key, label in {"signStartDate": "签到开始", "signEndDate": "签到结束", "signOutStartDate": "签退开始", "signOutEndDate": "签退结束"}.items():
            field(label, date((meta.get("sign_windows") or {}).get(key)))
        field("退选截止", date(meta.get("cancel_ends_at")))
        field("作业截止", date(meta.get("homework_ends_at")))
    if change and change["type"] == "course-change":
        left, right = course_fields(change.get("before") or {}), course_fields(change.get("after") or {})
        field("变更类别", "、".join(CHANGE_LABELS[key] for key in change["categories"]))
        for key in change["categories"]:
            if key == "time":
                field("原时间", f"{date(left['starts_at']) or '未知'} 至 {date(left['ends_at']) or '未知'}")
                field("现时间", f"{date(right['starts_at']) or '未知'} 至 {date(right['ends_at']) or '未知'}")
            elif key in {"location", "teacher", "title"}:
                field(CHANGE_LABELS[key], f"{left[key] or '未提供'} → {right[key] or '未提供'}")
    if change and change["type"] == "assignment-grade":
        old = (change.get("before") or {}).get("metadata") or {}
        field("原得分", old.get("earned_score"))
    if meta.get("grade_comment"):
        field("教师评语", excerpt(meta["grade_comment"], 1200))
    if meta.get("description"):
        field("简介" if event["kind"] in {"boya", "boya_catalog"} else "正文摘要", excerpt(meta["description"], 600))
    url = meta.get("school_url") or {"spoc": "https://spoc.buaa.edu.cn", "judge": "https://judge.buaa.edu.cn", "bykc": "https://bykc.buaa.edu.cn/system/course-select"}.get(event.get("source"))
    if url:
        if context.get("network_mode") == "webvpn":
            from app.upstream.webvpn import to_webvpn_url
            url = to_webvpn_url(url)
        field("学校入口", url)
    field("规则", context.get("rule_name"))
    field("数据更新时间", date(meta.get("read_at") or context.get("event_updated_at")))
    field("时区", timezone)
    return "\n".join(lines)
