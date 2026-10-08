const state = {
  user: null, school: null,
  data: { events: [], sources: [], rules: [], channels: [], deliveries: [], inbox: [], preferences: {}, triggers: [], templates: [], connectors: [] },
  page: "today", view: "week", cursor: new Date(), authMode: "login",
  taskStatus: "incomplete", taskSource: "", taskType: "", taskHistory: false, showCompleted: false,
  syncPending: new Set(),
  weekScrollTop: 8 * 56, weekScrollLeft: 0,
};
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const esc = (value = "") => String(value).replace(/[&<>'"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c]));
const kindMeta = {
  course: ["课程", "bi-journal-bookmark"], signin: ["签到", "bi-person-check"],
  assignment: ["作业", "bi-code-square"], exam: ["考试", "bi-pencil-square"],
  grade: ["成绩", "bi-mortarboard"],
  fitness: ["体育", "bi-activity"], booking: ["预约", "bi-bookmark-check"],
  boya: ["博雅", "bi-compass"], signout: ["签退", "bi-box-arrow-right"],
  venue: ["场馆", "bi-building"],
  custom: ["日程", "bi-calendar-event"],
};

function cookie(name) {
  return document.cookie.split("; ").find((part) => part.startsWith(`${name}=`))?.split("=").slice(1).join("=") || "";
}

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = { ...(options.headers || {}) };
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) headers["X-CSRF-Token"] = decodeURIComponent(cookie("puaa_csrf"));
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  let data = null;
  if (response.status !== 204) data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = Array.isArray(data?.detail) ? data.detail.map((item) => item.msg).join("；") : data?.detail;
    const error = new Error(detail || "request_failed");
    error.status = response.status;
    throw error;
  }
  return data;
}

function dateOf(value) { const d = value ? new Date(value) : null; return d && !Number.isNaN(d.getTime()) ? d : null; }
function anchor(event) { return event.starts_at || event.due_at || event.ends_at; }
function sameDay(a, b) { return a && b && a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate(); }
function startOfWeek(date) { const d = new Date(date); const day = d.getDay() || 7; d.setHours(0,0,0,0); d.setDate(d.getDate() - day + 1); return d; }
function addDays(date, count) { const d = new Date(date); d.setDate(d.getDate() + count); return d; }
function fmtTime(value) { const d = dateOf(value); return d ? new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }).format(d) : "全天"; }
function fmtDate(date, options = { month: "numeric", day: "numeric" }) { return new Intl.DateTimeFormat("zh-CN", options).format(date); }
function eventDate(event) { return dateOf(anchor(event)); }
function isCompleted(event) { return event.status === "done" || ["submitted", "completed"].includes(event.completion_state); }
function isExpired(event) { const end = dateOf(event.kind === "assignment" ? event.due_at : event.ends_at); return Boolean(end && end < new Date()); }
function statusLabel(event) {
  if (event.ignored) return "已忽略";
  if (event.kind === "course") return event.status_label || (isExpired(event) ? "已结束" : dateOf(event.starts_at) && dateOf(event.starts_at) <= new Date() ? "上课中" : "待上课");
  let label = event.status_label || ({ done: event.kind === "signin" ? "已签到" : "已提交", pending: "未提交", missing: "未签到", partial: "部分提交" })[event.status] || "状态未知";
  const meta = event.metadata || {};
  if (meta.total_problems) label += ` ${meta.submitted_count || 0}/${meta.total_problems}`;
  return label;
}
function statusBadge(event) { return `<span class="task-status ${event.ignored ? "ignored" : isCompleted(event) ? "complete" : ""}">${esc(statusLabel(event))}</span>`; }
function visibleCalendarEvents() { return state.data.events.filter((event) => !event.ignored && (state.showCompleted || !isCompleted(event))); }
function sourceLabel(id) { return ({ spoc: "SPOC", judge: "JUDGE", byxt: "课程提醒", iclass: "课程签到" })[id] || id; }
function assignmentTypeLabel(type) { return /^\d+$/.test(String(type)) ? `类型 ${type}` : String(type); }
function typeOptions(selected = "", source = "") {
  const types = [...new Set(state.data.events.filter((event) => event.kind === "assignment" && (!source || event.source === source)).map((event) => event.metadata?.assignment_type).filter(Boolean))].sort();
  if (selected && !types.includes(selected)) types.push(selected);
  return `<option value="">全部类型</option>` + types.map((type) => `<option value="${esc(type)}" ${type === selected ? "selected" : ""}>${esc(assignmentTypeLabel(type))}</option>`).join("");
}
function eventTeacher(event) {
  const meta = event.metadata || {};
  let value = meta.teacherName || meta.teacherNames || meta.teacherNameStr || meta.courseTeacher || meta.teacher || meta.jsxm || meta.teachers;
  if (!value && meta.weeksAndTeachers) value = String(meta.weeksAndTeachers).split("/").slice(1).map((part) => part.replace(/\[[^\]]*\]/g, "").trim()).filter(Boolean).join("、");
  if (Array.isArray(value)) return value.map((item) => typeof item === "object" ? (item.name || item.teacherName || "") : item).filter(Boolean).join("、");
  if (value && typeof value === "object") return value.name || value.teacherName || "";
  return value ? String(value) : "";
}
function eventTimeRange(event) {
  const start = fmtTime(event.starts_at || event.due_at || event.ends_at);
  const end = event.ends_at && event.starts_at ? fmtTime(event.ends_at) : "";
  return end && end !== start ? `${start}–${end}` : start;
}
function eventDetails(event) {
  return [eventTeacher(event), event.location].filter(Boolean).join("　");
}

function empty(title, detail, icon = "bi-calendar2") {
  return `<div class="empty"><i class="bi ${icon}"></i><strong>${esc(title)}</strong><p>${esc(detail)}</p></div>`;
}

function toast(message) {
  const el = $("#toast"); el.textContent = message; el.classList.add("show");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove("show"), 1900);
}

function showAuth() { $("#auth-screen").hidden = false; $("#app-shell").hidden = true; }
function showApp() { $("#auth-screen").hidden = true; $("#app-shell").hidden = false; }

async function bootstrap() {
  try {
    const session = await api("/api/session");
    state.user = session.user; state.school = session.school; showApp(); await loadDashboard();
    const initial = location.hash.slice(1); navigate(["today","calendar","tasks","alerts","connections","channels","account"].includes(initial) ? initial : "today");
  } catch (error) {
    if (error.status === 401) showAuth(); else toast("服务暂不可用");
  }
}

async function loadDashboard() {
  const [dashboard, templates, triggers, connectors] = await Promise.all([
    api("/api/dashboard"), api("/api/rule-templates"), api("/api/triggers"), api("/api/connectors"),
  ]);
  state.data = { ...dashboard, templates, triggers, connectors };
  state.school = state.data.school;
  renderShell(); renderAll();
  if (state.data.sources.some((source) => source.syncing)) scheduleSyncPoll();
}

function renderShell() {
  const name = state.user.display_name;
  $("#user-name").textContent = name;
  $("#user-avatar").textContent = name.slice(0, 1).toUpperCase();
  $("#school-state").textContent = state.school?.status === "connected" ? `${state.school.school_id} · 已连接` : state.school?.status === "degraded" ? `${state.school.school_id} · 部分异常` : state.school?.status === "login_required" ? "需要重新登录" : "未连接北航";
  $("#account-name").textContent = name; $("#account-email").textContent = state.user.email;
  $("#today-label").textContent = fmtDate(new Date(), { year: "numeric", month: "long", day: "numeric", weekday: "long" });
  const availableSources = state.data.sources.filter((s) => s.available !== false);
  const healthy = availableSources.filter((s) => s.status === "healthy").length;
  $("#source-summary").textContent = `${healthy}/${availableSources.length} 个来源正常`;
}

function renderAll() { renderToday(); renderCalendar(); renderTasks(); renderRules(); renderConnections(); renderChannels(); renderPreferences(); updatePeriodTitle(); }

function renderToday() {
  const selected = new Date(); selected.setHours(0,0,0,0);
  const now = new Date(), weekEnd = addDays(selected, 7);
  const actionable = state.data.events.filter((event) => !event.ignored && !isCompleted(event) && !isExpired(event));
  const events = actionable.filter((event) => sameDay(eventDate(event), selected)).sort((a,b) => eventDate(a)-eventDate(b));
  const deadlines = actionable.filter((event) => event.due_at && dateOf(event.due_at) >= now && dateOf(event.due_at) < weekEnd).sort((a,b) => dateOf(a.due_at)-dateOf(b.due_at));
  $("#today-label").textContent = fmtDate(selected, { year: "numeric", month: "long", day: "numeric", weekday: "long" });
  $("#today-summary").innerHTML = [
    ["今日待提交", events.filter((event) => event.kind === "assignment").length, "bi-journal-text"],
    ["今日课程", events.filter((event) => event.kind === "course").length, "bi-journal-bookmark"],
    ["本周截止", deadlines.length, "bi-hourglass-split"]
  ].map(([label,count,icon]) => `<button type="button" class="today-metric" data-open-tasks><i class="bi ${icon}"></i><span>${label}</span><strong>${count}</strong></button>`).join("");
  $$("[data-open-tasks]").forEach((button) => button.addEventListener("click", () => {
    state.taskStatus = "incomplete"; state.taskSource = ""; state.taskType = ""; state.taskHistory = false;
    $("#task-source").value = ""; $("#task-history").checked = false; renderTasks(); navigate("tasks");
  }));
  $("#today-timeline").innerHTML = events.length ? events.map((event) => `<article class="timeline-row"><time>${eventTimeRange(event)}</time><span class="timeline-dot"></span><div class="timeline-main"><strong>${esc(event.title)}</strong><span>${esc(sourceLabel(event.source))}${eventDetails(event) ? ` · ${esc(eventDetails(event))}` : ""}</span>${statusBadge(event)}</div></article>`).join("") : empty("今天没有待处理事项", "后续安排可在日历查看", "bi-check2-circle");
  $("#deadline-list").innerHTML = deadlines.slice(0,5).map((event) => `<div class="deadline-item"><strong>${esc(event.title)}</strong><span>${fmtDate(dateOf(event.due_at), { year:"numeric", month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit" })} · ${esc(statusLabel(event))}</span></div>`).join("") || `<div class="empty"><i class="bi bi-check2"></i><p>7 天内没有截止作业</p></div>`;
}

function eventsForDay(date) { return visibleCalendarEvents().filter((event) => sameDay(eventDate(event), date)); }

const WEEK_HOUR_HEIGHT = 56;
function weekPinned(event) { return event.kind === "assignment" || event.all_day; }
function weekPinnedDate(event) { return dateOf(event.kind === "assignment" ? event.due_at || anchor(event) : anchor(event)); }

// Clip spanning events to each local day, and allocate lanes using their painted
// height (not just duration) so short or simultaneous entries never cover one another.
function weekDayLayout(day) {
  const next = addDays(day, 1);
  const items = visibleCalendarEvents().filter((event) => !weekPinned(event)).flatMap((event) => {
    const start = eventDate(event); if (!start) return [];
    const givenEnd = dateOf(event.ends_at);
    const end = givenEnd && givenEnd > start ? givenEnd : new Date(start.getTime() + 45 * 60000);
    if (start >= next || end <= day) return [];
    const top = Math.max(0, (start - day) / 3600000 * WEEK_HOUR_HEIGHT);
    const actualBottom = Math.min(24 * WEEK_HOUR_HEIGHT, (end - day) / 3600000 * WEEK_HOUR_HEIGHT);
    const height = Math.min(Math.max(42, actualBottom - top), 24 * WEEK_HOUR_HEIGHT);
    const paintedTop = Math.min(top, 24 * WEEK_HOUR_HEIGHT - height);
    return [{ event, top: paintedTop, height, bottom: paintedTop + height, continued: start < day || end > next }];
  }).sort((a,b) => a.top - b.top || b.height - a.height);
  let group = [], groupEnd = -1;
  const finish = () => {
    const ends = [];
    for (const item of group) {
      let lane = ends.findIndex((end) => end <= item.top);
      if (lane < 0) lane = ends.length;
      ends[lane] = item.bottom; item.lane = lane;
    }
    group.forEach((item) => item.lanes = ends.length);
    group = [];
  };
  for (const item of items) {
    if (item.top >= groupEnd) finish();
    group.push(item); groupEnd = Math.max(groupEnd, item.bottom);
  }
  finish(); return items;
}

function renderWeek() {
  const start = startOfWeek(state.cursor), now = new Date();
  const days = Array.from({ length: 7 }, (_, i) => addDays(start, i));
  const heads = days.map((d) => `<span class="${sameDay(d, now) ? "is-today" : ""}">${"一二三四五六日"[(d.getDay() || 7) - 1]}<b>${d.getDate()}</b></span>`).join("");
  const axis = Array.from({ length: 24 }, (_, i) => `<span>${String(i).padStart(2, "0")}:00</span>`).join("") + `<span class="time-end">24:00</span>`;
  const pinned = days.map((day) => {
    const items = visibleCalendarEvents().filter((event) => weekPinned(event) && sameDay(weekPinnedDate(event), day)).sort((a,b) => weekPinnedDate(a)-weekPinnedDate(b));
    return `<div class="week-pinned-day">${items.map((event) => `<button type="button" class="week-deadline" data-calendar-event="${event.id}" title="${esc(event.title)}"><time>${event.all_day && event.kind !== "assignment" ? "全天" : fmtTime(event.due_at || anchor(event))}</time><strong>${esc(event.title)}</strong><span>${esc(statusLabel(event))}</span></button>`).join("") || `<span class="week-no-deadline">—</span>`}</div>`;
  }).join("");
  const columns = days.map((day) => {
    const items = weekDayLayout(day).map(({event,top,height,lane,lanes,continued}) => {
      const details = eventDetails(event);
      return `<button type="button" class="calendar-event" data-calendar-event="${event.id}" title="${esc(event.title)}" style="top:${top}px;height:${height}px;left:calc(${lane / lanes * 100}% + 4px);width:calc(${100 / lanes}% - 8px)"><strong>${esc(event.title)}</strong><span>${eventTimeRange(event)}${continued ? " · 跨日" : ""}</span><span>${esc(statusLabel(event))}</span>${details ? `<span>${esc(details)}</span>` : ""}</button>`;
    }).join("");
    const current = sameDay(day, now) ? `<div class="week-now" style="top:${(now.getHours() + now.getMinutes()/60) * WEEK_HOUR_HEIGHT}px" aria-label="当前时间 ${fmtTime(now)}"></div>` : "";
    return `<div class="day-column">${items}${current}</div>`;
  }).join("");
  return `<div class="week-sticky"><div class="week-head"><span>时间</span>${heads}</div><div class="week-pinned"><span class="week-pinned-label">截止<br>／全天</span>${pinned}</div></div><div class="week-body"><div class="time-axis">${axis}</div>${columns}</div>`;
}

function openCalendarDetail(id) {
  const event = visibleCalendarEvents().find((item) => String(item.id) === id);
  if (!event) return;
  $("#calendar-detail-title").textContent = event.title;
  const date = weekPinnedDate(event) || eventDate(event);
  const fields = [["来源", sourceLabel(event.source)], ["日期", date ? fmtDate(date, {year:"numeric",month:"long",day:"numeric",weekday:"long"}) : "时间未知"], [event.kind === "assignment" ? "截止" : "时间", eventTimeRange(event)], ["状态", statusLabel(event)], ["教师", eventTeacher(event)], ["教室", event.location]].filter(([,value]) => value);
  $("#calendar-detail-body").innerHTML = fields.map(([label,value]) => `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join("");
  $("#calendar-detail-dialog").showModal();
}

function renderMonth() {
  const first = new Date(state.cursor.getFullYear(), state.cursor.getMonth(), 1); const start = startOfWeek(first);
  const cells = Array.from({ length: 42 }, (_, i) => { const d = addDays(start, i); const outside = d.getMonth() !== first.getMonth(); return `<div class="month-day ${outside ? "outside" : ""}"><b>${d.getDate()}</b>${eventsForDay(d).slice(0, 4).map((e) => `<span class="month-event">${fmtTime(anchor(e))} ${esc(e.title)} · ${esc(statusLabel(e))}</span>`).join("")}</div>`; }).join("");
  return `<div class="month-grid">${cells}</div>`;
}

function renderAgenda() {
  const groups = new Map();
  visibleCalendarEvents().forEach((event) => { const d = eventDate(event); if (!d) return; const key = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`; if (!groups.has(key)) groups.set(key, { date: d, events: [] }); groups.get(key).events.push(event); });
  return groups.size ? [...groups.values()].sort((a,b) => a.date-b.date).map((group) => `<section class="agenda-group"><h3>${fmtDate(group.date, { year: "numeric", month: "long", day: "numeric", weekday: "long" })}</h3>${group.events.map((event) => `<div class="agenda-row"><b>${eventTimeRange(event)}</b><strong>${esc(event.title)} ${statusBadge(event)}</strong><span>${esc(eventTeacher(event) || "—")}</span><span>${esc(event.location || "—")}</span></div>`).join("")}</section>`).join("") : empty("日历还是空的", "连接学校账号后，课程与考试会出现在这里");
}

function renderDay() {
  const events = eventsForDay(state.cursor);
  return events.length ? `<section class="agenda-group"><h3>${fmtDate(state.cursor, { year: "numeric", month: "long", day: "numeric", weekday: "long" })}</h3>${events.map((event) => `<div class="agenda-row"><b>${eventTimeRange(event)}</b><strong>${esc(event.title)} ${statusBadge(event)}</strong><span>${esc(eventTeacher(event) || "—")}</span><span>${esc(event.location || "—")}</span></div>`).join("")}</section>` : empty("这一天没有日程", "可以新建个人日程");
}

function renderCalendar() {
  const canvas = $("#calendar-canvas");
  if (canvas.dataset.view === "week" && state.page === "calendar" && canvas.clientHeight > 0) { state.weekScrollTop = canvas.scrollTop; state.weekScrollLeft = canvas.scrollLeft; }
  $("#event-count").textContent = `${visibleCalendarEvents().length} 项 · ${state.data.academic_window?.enabled ? `${state.data.academic_window.start} 至 ${state.data.academic_window.end || "不限"}` : "全部学期"}`;
  canvas.classList.toggle("is-week", state.view === "week"); canvas.dataset.view = state.view;
  canvas.innerHTML = state.view === "week" ? renderWeek() : state.view === "month" ? renderMonth() : state.view === "agenda" ? renderAgenda() : renderDay();
  $("#week-time-tools").hidden = state.view !== "week";
  if (state.view === "week") { canvas.scrollTop = state.weekScrollTop; canvas.scrollLeft = state.weekScrollLeft; }
  else { canvas.scrollTop = 0; canvas.scrollLeft = 0; }
  $$("[data-calendar-event]").forEach((button) => button.addEventListener("click", () => openCalendarDetail(button.dataset.calendarEvent)));
  $$('[data-view]').forEach((button) => button.classList.toggle("active", button.dataset.view === state.view));
}

function renderTasks() {
  const tasks = state.data.events.filter((event) => {
    if (!["assignment", "course"].includes(event.kind)) return false;
    if (state.taskSource && event.source !== state.taskSource) return false;
    if (state.taskType && event.metadata?.assignment_type !== state.taskType) return false;
    if (!state.taskHistory && state.taskStatus !== "ignored" && isExpired(event)) return false;
    if (state.taskStatus === "ignored") return Boolean(event.ignored);
    if (state.taskStatus !== "any" && event.ignored) return false;
    if (state.taskStatus === "incomplete") return !isCompleted(event);
    if (state.taskStatus === "submitted") return isCompleted(event);
    return state.taskStatus === "any" || event.completion_state === state.taskStatus;
  }).sort((a, b) => (eventDate(a)?.getTime() ?? Infinity) - (eventDate(b)?.getTime() ?? Infinity));
  $("#task-count").textContent = `${tasks.length} 项`;
  $("#task-type").innerHTML = typeOptions(state.taskType, state.taskSource);
  $$("[data-task-status]").forEach((button) => button.classList.toggle("active", button.dataset.taskStatus === state.taskStatus));
  $("#task-list").innerHTML = tasks.length ? tasks.map((event) => {
    const date = eventDate(event), metadata = event.metadata || {}, submittedAt = dateOf(metadata.submitted_at);
    return `<div class="list-row task-row"><span class="row-icon"><i class="bi ${kindMeta[event.kind][1]}"></i></span><div><strong>${esc(event.title)}</strong><small>${esc(sourceLabel(event.source))}${metadata.assignment_type ? ` · ${esc(assignmentTypeLabel(metadata.assignment_type))}` : ""}${eventTeacher(event) ? ` · ${esc(eventTeacher(event))}` : ""}</small><small class="task-date">${date ? fmtDate(date, { year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "时间未知"}${isExpired(event) ? " · 已过期" : ""}</small>${submittedAt ? `<small>提交于 ${fmtDate(submittedAt, { year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}</small>` : ""}</div>${statusBadge(event)}<button class="quiet-button ignore-button" type="button" data-ignore-event="${event.id}" data-ignored="${Boolean(event.ignored)}"><i class="bi ${event.ignored ? "bi-arrow-counterclockwise" : "bi-eye-slash"}"></i>${event.ignored ? "取消忽略" : "忽略"}</button></div>`;
  }).join("") : empty("没有符合筛选的待办", "可切换状态、学期或过期筛选", "bi-list-check");
  $$("[data-ignore-event]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const ignored = button.dataset.ignored !== "true";
      await api(`/api/events/${button.dataset.ignoreEvent}/ignore`, { method:"PATCH", body:JSON.stringify({ ignored }) });
      await loadDashboard(); toast(ignored ? "已忽略，不再提醒" : "已取消忽略");
    } catch (error) { button.disabled = false; toast(error.message); }
  }));
}

const anchorLabels = { starts_at: "开始时间", ends_at: "结束时间", due_at: "截止时间" };
const channelLabels = { in_app: "站内", email: "邮件", sms_webhook: "短信 Webhook", wxpusher: "WxPusher", telegram: "Telegram" };
function durationText(minutes) { const n = Math.abs(minutes); return n % 1440 === 0 && n >= 1440 ? `${n / 1440} 天` : n % 60 === 0 && n >= 60 ? `${n / 60} 小时` : `${n} 分钟`; }
const reminderProfiles = {
  "spoc-assignment": { name: "SPOC 作业截止提醒", icon: "bi-journal-text", summary: "按设置的多个时间点提醒" },
  "judge-assignment": { name: "JUDGE 作业截止提醒", icon: "bi-code-square", summary: "按设置的多个时间点提醒" },
  course: { name: "课程提醒", icon: "bi-journal-bookmark", summary: "上课前或课程结束前提醒" },
  "course-change": { name: "课程变更提醒", icon: "bi-arrow-repeat", summary: "同步对比课程安排" },
  "spoc-new-assignment": { name: "SPOC 新作业提醒", icon: "bi-journal-plus", summary: "同步发现新作业时提醒" },
  "judge-new-assignment": { name: "JUDGE 新作业提醒", icon: "bi-file-earmark-plus", summary: "同步发现新作业时提醒" },
  "spoc-assignment-grade": { name: "SPOC 作业新评分提醒", icon: "bi-check2-square", summary: "教师批阅、得分或评语更新时提醒" },
  signin: { name: "课程签到提醒", icon: "bi-person-check", summary: "结束前检测签到状态" },
  library: { name: "图书馆预约提醒", icon: "bi-bookmark-check", summary: "开始与离开失效前提醒" },
  assignment: { name: "作业截止提醒", icon: "bi-code-square", summary: "截止前分级提醒" },
  exam: { name: "考试提醒", icon: "bi-pencil-square", summary: "考试前分级提醒" },
  grade: { name: "新成绩提醒", icon: "bi-mortarboard", summary: "成绩发布时提醒" },
  fitness: { name: "阳光体育提醒", icon: "bi-activity", summary: "未达标且临近截止时提醒" },
  "boya-course": { name: "博雅课程提醒", icon: "bi-compass", summary: "博雅课程开始前提醒" },
  "boya-signout": { name: "博雅签退提醒", icon: "bi-box-arrow-right", summary: "签退结束前检查状态" },
  venue: { name: "体育场馆提醒", icon: "bi-building", summary: "预约开始与结束前提醒" },
  custom: { name: "自定义日程提醒", icon: "bi-sliders", summary: "按日程时间提醒" },
};
// Each connector owns its second-level types, not a shared three-type template.
const connectionRuleTypes = {
  byxt: [["course", "课程提醒"], ["course-change", "课程变更提醒"]],
  spoc: [["spoc-assignment", "作业截止提醒"], ["spoc-new-assignment", "新作业提醒"], ["spoc-assignment-grade", "作业新评分提醒"]],
  judge: [["judge-assignment", "作业截止提醒"], ["judge-new-assignment", "新作业提醒"]],
  assignments: [["assignment", "作业截止提醒（旧规则）"]],
};
const deadlineProfiles = new Set(["assignment", "spoc-assignment", "judge-assignment"]);
const changeProfiles = new Set(["course-change", "spoc-new-assignment", "judge-new-assignment", "spoc-assignment-grade"]);
const courseChangeTypes = {time:"时间变更",location:"教室变更",teacher:"教师变更",title:"名称变更",added:"新增课程",cancelled:"课程取消"};
const activeProfiles = new Set(Object.values(connectionRuleTypes).flat().map(([key]) => key));
function connectionForProfile(profile) { return Object.keys(connectionRuleTypes).find(key => connectionRuleTypes[key].some(([value]) => value === profile)) || "spoc"; }
function renderRuleTypes(connection, selected) {
  const choices = connectionRuleTypes[connection] || [];
  $("#rule-profile").innerHTML = choices.map(([value, label]) => `<option value="${value}">${label}</option>`).join("");
  $("#rule-profile").value = choices.some(([value]) => value === selected) ? selected : choices[0]?.[0];
}
function advanceMinutes(point) {
  const values = [point.days, point.hours, point.minutes].map(value => Number(value ?? 0));
  const [days, hours, minutes] = values;
  if (values.some(value => !Number.isInteger(value) || value < 0) || days > 365 || hours > 23 || minutes > 59) throw new Error("小时为 0–23，分钟为 0–59；请填写非负整数");
  const total = days * 1440 + hours * 60 + minutes;
  if (total < 1 || total > 525600) throw new Error("必须至少提前 1 分钟，不能在截止时提醒");
  return total;
}
function advancePoint(minutes) { return {days:Math.floor(minutes/1440), hours:Math.floor(minutes%1440/60), minutes:minutes%60}; }
function advanceText(point) { return [[point.days,"天"],[point.hours,"小时"],[point.minutes,"分钟"]].filter(([n]) => n > 0).map(([n,u]) => `${n} ${u}`).join(" "); }
function deadlinePoints(rule) {
  const settings = rule?.profile_settings || {};
  if (settings.reminder_times?.length) return settings.reminder_times;
  const triggers = rule?.triggers?.length ? rule.triggers : rule?.trigger ? [rule.trigger] : [];
  if (triggers.length) {
    const actions = rule.actions?.length ? rule.actions : [{after_minutes:0}];
    return [...new Set(triggers.flatMap(t => actions.map(a => -(t.offset_minutes + (a.after_minutes || 0)))).filter(n => n > 0))].map(advancePoint);
  }
  return [...new Set([settings.days_before === undefined ? 1440 : settings.days_before * 1440, settings.hours_before === undefined ? 120 : settings.hours_before * 60].filter(n => n > 0))].map(advancePoint);
}
function inferRuleProfile(rule) {
  if (rule.profile && reminderProfiles[rule.profile]) return rule.profile;
  const kind = rule.scope?.kinds?.[0]; const source = rule.scope?.sources?.[0];
  if (kind === "assignment" && source === "spoc") return "spoc-assignment";
  if (kind === "assignment" && source === "judge") return "judge-assignment";
  if (source === "libbook") return "library";
  if (source === "cgyy" || kind === "venue") return "venue";
  if (source === "bykc" && kind === "signout") return "boya-signout";
  if (source === "bykc" || kind === "boya") return "boya-course";
  return ({ course: "course", signin: "signin", assignment: "assignment", exam: "exam", grade: "grade", fitness: "fitness" })[kind] || "custom";
}
function profileSentence(rule) {
  const profile = inferRuleProfile(rule); const s = rule.profile_settings || {};
  if (profile === "course") { const settings = settingsFromRule(rule, profile); return `${settings.timing === "before_end" ? "课程结束前" : "上课前"} ${settings.advance_minutes} 分钟提醒`; }
  if (profile === "signin") return `课程结束前 ${s.before_end ?? Math.abs(rule.trigger?.offset_minutes || 30)} 分钟检测，未签到提醒`;
  if (profile === "library") return `开始前 ${s.before_start ?? 30} 分钟，离开失效前 ${s.before_expiry ?? 15} 分钟提醒`;
  if (deadlineProfiles.has(profile)) return `截止前 ${deadlinePoints(rule).map(advanceText).join("、")}提醒`;
  if (profile === "course-change") return `同步发现${(s.change_types || []).map(key => courseChangeTypes[key]).join("、")}时提醒`;
  if (profile === "spoc-assignment-grade") return "教师批阅后提醒；得分或评语更新也提醒";
  if (changeProfiles.has(profile)) return "同步发现新作业时提醒";
  if (profile === "exam") return `考试前 ${s.days_before ?? 7} 天、${s.hours_before ?? 24} 小时、${s.final_hours ?? 2} 小时提醒`;
  if (profile === "grade") return `每 ${s.poll_interval ?? 15} 分钟检查，新成绩出现或变化时提醒`;
  if (profile === "fitness") return `每周${"一二三四五六日"[(s.check_weekday ?? 1) - 1] || "一"} ${String(s.check_hour ?? 9).padStart(2, "0")}:00 检查，仅在每周打满才够时提醒`;
  if (profile === "boya-course") return `课程开始前 ${s.before_start ?? 30} 分钟提醒`;
  if (profile === "boya-signout") return `签退结束前 ${s.before_end ?? 20} 分钟检测`;
  if (profile === "venue") return `预约开始前 ${s.before_start ?? 60} 分钟、结束前 ${s.before_end ?? 15} 分钟提醒`;
  return ruleSentence(rule);
}
function ruleSentence(rule) {
  const kinds = rule.scope.kinds.length ? rule.scope.kinds.map((k) => (kindMeta[k] || [k])[0]).join("、") : "全部事项";
  const offset = rule.trigger.offset_minutes;
  const timing = `${anchorLabels[rule.trigger.anchor]}${offset < 0 ? "前" : offset > 0 ? "后" : "时"}${offset ? durationText(offset) : ""}`;
  const conditions = rule.conditions.length ? `，满足 ${rule.conditions.map((c) => `${c.field} ${c.op} ${c.value ?? ""}`).join(" 且 ")}` : "";
  const actions = rule.actions.map((a) => `${a.after_minutes ? `${a.after_minutes} 分钟后 ` : ""}${channelLabels[a.channel] || a.channel}`).join("，再通过 ");
  return `${kinds}在${timing}${conditions}，通过 ${actions} 提醒`;
}
function renderRules() {
  $("#rule-list").innerHTML = state.data.rules.length ? state.data.rules.map((rule) => { const profile = reminderProfiles[inferRuleProfile(rule)] || reminderProfiles.custom; const available = rule.available !== false; return `<article class="rule-card ${available ? "" : "coming-soon-card"}" ${available ? `data-edit-rule="${rule.id}"` : 'aria-disabled="true"'}><span class="row-icon"><i class="bi ${profile.icon}"></i></span><div><strong>${esc(rule.name)}</strong><div class="rule-sentence">${available ? esc(profileSentence(rule)) : "Coming Soon"}</div></div><div class="rule-actions"><span class="rule-state ${rule.enabled ? "" : "off"}">${available ? (rule.enabled ? "运行中" : "已停用") : "Coming Soon"}</span><label class="switch" aria-label="启用${esc(rule.name)}"><input type="checkbox" ${available ? `data-rule-toggle="${rule.id}"` : "disabled"} ${rule.enabled ? "checked" : ""}><span></span></label></div></article>`; }).join("") : `<div class="empty"><i class="bi bi-bell"></i><strong>还没有提醒</strong><button class="primary-button" data-new-rule>新建提醒</button></div>`;
  $("#rule-list").innerHTML += Object.entries(reminderProfiles).filter(([key]) => !activeProfiles.has(key) && !state.data.rules.some((rule) => inferRuleProfile(rule) === key)).map(([, profile]) => `<article class="rule-card coming-soon-card" aria-disabled="true"><span class="row-icon"><i class="bi ${profile.icon}"></i></span><strong>${esc(profile.name)}</strong><span class="coming-soon-badge">Coming Soon</span></article>`).join("");
  $$('[data-edit-rule]').forEach((card) => card.addEventListener("click", (event) => { if (!event.target.closest(".switch")) openRuleDialog(state.data.rules.find((r) => r.id === Number(card.dataset.editRule))); }));
  $$('[data-rule-toggle]').forEach((input) => input.addEventListener("change", async () => { try { await api(`/api/rules/${input.dataset.ruleToggle}`, { method: "PATCH", body: JSON.stringify({ enabled: input.checked }) }); await loadDashboard(); toast("规则已保存"); } catch { input.checked = !input.checked; toast("保存失败"); } }));
  $$('[data-new-rule]').forEach((button) => button.addEventListener("click", () => openRuleDialog()));
  renderTriggers(); renderDeliveries(); renderInbox();
}

function renderTriggers() {
  $("#trigger-list").innerHTML = state.data.triggers.length ? state.data.triggers.map((item) => `<div class="list-row"><span class="row-icon"><i class="bi bi-clock-history"></i></span><div><strong>${esc(item.event_title)}</strong><small>${esc(item.rule_name)}</small></div><small>${fmtDate(dateOf(item.run_at), { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}</small><span>${esc(item.status)}</span></div>`).join("") : empty("没有待执行计划", "规则匹配日程后会生成计划", "bi-clock-history");
}
function renderDeliveries() {
  $("#delivery-list").innerHTML = state.data.deliveries.length ? state.data.deliveries.map((item) => `<div class="list-row"><span class="row-icon"><i class="bi bi-send"></i></span><div><strong>${esc(item.title)}</strong><small>${esc(channelLabels[item.channel] || item.channel)}${item.error ? ` · ${esc(item.error)}` : ""}</small></div><small>${fmtDate(dateOf(item.created_at), { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}</small><span>${esc(item.status)}</span></div>`).join("") : empty("还没有发送记录", "通知发送后会记录结果", "bi-send");
}

function renderInbox() {
  $("#inbox-list").innerHTML = state.data.inbox.length ? state.data.inbox.map((item) => `<div class="list-row"><span class="row-icon"><i class="bi bi-bell"></i></span><div><strong>${esc(item.title)}</strong><small>${esc(item.body)}</small></div><small>${fmtDate(dateOf(item.created_at), { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}</small></div>`).join("") : empty("还没有站内通知", "规则触发后会显示在这里", "bi-bell");
}

function renderSourceRow(source) {
  if (source.available === false) return `<div class="source-row coming-soon-source" aria-disabled="true"><strong>${esc(source.label)}</strong><span class="coming-soon-badge">Coming Soon</span><label class="switch" aria-label="${esc(source.label)} Coming Soon"><input type="checkbox" disabled><span></span></label></div>`;
  const interval = Number(source.refresh_interval_minutes ?? 60), syncing = source.syncing || state.syncPending.has(source.id), progress = source.sync_progress;
  const detail = syncing ? `${progress?.phase || "同步中"}${progress?.total ? ` ${progress.completed}/${progress.total}` : "…"}` : !source.enabled ? "已停用" : source.status === "healthy" ? `正常 · ${source.event_count} 项` : (source.detail || "未同步");
  const intervals = [[0, "手动"], [5, "5 分钟"], [15, "15 分钟"], [30, "30 分钟"], [60, "1 小时"], [360, "6 小时"], [1440, "每天"]];
  return `<div class="source-row active-source"><strong>${esc(source.label)}</strong><span class="source-detail" role="status">${esc(detail)}</span><span class="source-last-sync">${source.last_sync_at ? fmtDate(dateOf(source.last_sync_at), { year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "未同步"}</span><select class="source-interval" data-source-interval="${source.id}" aria-label="${esc(source.label)}刷新间隔" ${syncing ? "disabled" : ""}>${intervals.map(([value, label]) => `<option value="${value}" ${interval === value ? "selected" : ""}>${label}</option>`).join("")}</select><button type="button" class="quiet-button source-sync-button" data-source-sync="${source.id}" ${syncing || !source.enabled ? "disabled" : ""}><i class="bi bi-arrow-repeat ${syncing ? "is-spinning" : ""}"></i>${syncing ? "同步中" : "同步"}</button><label class="switch" aria-label="启用${esc(source.label)}"><input type="checkbox" data-source-toggle="${source.id}" ${source.enabled ? "checked" : ""} ${syncing ? "disabled" : ""}><span></span></label></div>`;
}

function renderConnections() {
  const attached = Boolean(state.school?.school_id); const needsLogin = state.school?.status === "login_required";
  const statusText = !attached ? "未连接" : needsLogin ? (state.school.detail || "需要重新登录") : `${state.school.school_id} · ${state.school.remember_password ? "按来源自动刷新" : "仅当前会话"}`;
  $("#school-connection").innerHTML = `<div class="connection-main"><i class="bi ${needsLogin ? "bi-shield-exclamation" : "bi-shield-check"}"></i><div><strong>${attached ? esc(state.school.display_name || state.school.school_id) : "北航统一认证"}</strong><span>${esc(statusText)}</span></div></div><button class="${attached && !needsLogin ? "danger-button" : "primary-button"}" id="school-action">${attached && !needsLogin ? "断开" : needsLogin ? "重新连接" : "连接"}</button>`;
  $("#school-action").addEventListener("click", attached && !needsLogin ? disconnectSchool : openSchoolDialog);
  $("#source-list").innerHTML = [...state.data.sources].sort((a, b) => Number(b.available !== false) - Number(a.available !== false)).map(renderSourceRow).join("");
  $$("[data-source-sync]").forEach((button) => button.addEventListener("click", () => syncSources(button, button.dataset.sourceSync)));
  const syncing = state.data.sources.some((source) => source.syncing);
  $("#source-sync").disabled = syncing || state.syncPending.size > 0;
  $("#source-sync").textContent = syncing ? "同步中…" : "同步全部";
  $("#sync-button").disabled = syncing || state.syncPending.size > 0;
  $("#sync-button").classList.toggle("is-spinning", syncing);
  $$('[data-source-toggle]').forEach((input) => input.addEventListener("change", async () => { try { await api(`/api/sources/${encodeURIComponent(input.dataset.sourceToggle)}`, { method: "PATCH", body: JSON.stringify({ enabled: input.checked }) }); await loadDashboard(); toast(input.checked ? "来源已启用，下次同步生效" : "来源已停用"); } catch (e) { input.checked = !input.checked; toast(e.message || "保存失败"); } }));
  $$('[data-source-interval]').forEach((select) => select.addEventListener("change", async () => { try { await api(`/api/sources/${encodeURIComponent(select.dataset.sourceInterval)}`, { method: "PATCH", body: JSON.stringify({ refresh_interval_minutes: Number(select.value) }) }); await loadDashboard(); toast(select.value === "0" ? "已改为手动刷新" : "刷新频率已保存"); } catch (e) { toast(e.message || "保存失败"); } }));
}

function renderChannels() {
  const builtIn = `<article class="channel-card"><div class="channel-card-head"><span class="row-icon"><i class="bi bi-app-indicator"></i></span><span class="rule-state">可用</span></div><h3>站内通知</h3><p>PUAA 通知中心</p><div class="channel-card-actions"><button class="quiet-button" data-test-in-app>测试</button></div></article>`;
  const configured = state.data.channels.map((channel) => `<article class="channel-card"><div class="channel-card-head"><span class="row-icon"><i class="bi ${channel.type === "email" ? "bi-envelope" : channel.type === "telegram" ? "bi-telegram" : channel.type === "wxpusher" ? "bi-wechat" : "bi-phone"}"></i></span><span class="rule-state ${channel.status === "error" ? "off" : ""}">${channel.status === "healthy" ? "正常" : channel.status === "error" ? "异常" : "未验证"}</span></div><h3>${esc(channel.name)}</h3><p>${esc(channelLabels[channel.type] || channel.type)}${channel.last_error ? ` · ${esc(channel.last_error)}` : ""}</p><div class="channel-card-actions"><button class="quiet-button" data-test-channel="${channel.id}">测试</button><button class="danger-button" data-delete-channel="${channel.id}">删除</button></div></article>`).join("");
  $("#channel-list").innerHTML = builtIn + configured;
  $("[data-test-in-app]").addEventListener("click", async () => { try { await api("/api/channels/test-in-app", { method: "POST" }); await loadDashboard(); toast("测试通知已发送"); } catch (e) { toast(e.message); } });
  $$('[data-test-channel]').forEach((button) => button.addEventListener("click", async () => { try { await api(`/api/channels/${button.dataset.testChannel}/test`, { method: "POST" }); await loadDashboard(); toast("测试通知已发送"); } catch (e) { toast(e.message); } }));
  $$('[data-delete-channel]').forEach((button) => button.addEventListener("click", async () => { if (!confirm("删除这个通知渠道？使用它的规则将发送失败。")) return; await api(`/api/channels/${button.dataset.deleteChannel}`, { method: "DELETE" }); await loadDashboard(); toast("渠道已删除"); }));
}

function renderPreferences() {
  const prefs = state.data.preferences || {};
  $("#preferences-form [name=quiet_start]").value = prefs.quiet_start || "";
  $("#preferences-form [name=quiet_end]").value = prefs.quiet_end || "";
  const window = state.data.academic_window || {}, form = $("#academic-form");
  if (!form.contains(document.activeElement)) {
    form.elements.academic_filter_enabled.checked = prefs.academic_filter_enabled !== false;
    form.elements.academic_mode.value = window.mode || "auto";
    form.elements.academic_start_date.value = prefs.academic_start_date || window.start || "";
    form.elements.academic_end_date.value = prefs.academic_end_date || window.end || "";
    updateAcademicControls();
  }
  const range = window.enabled ? `${window.start} 至 ${window.end || "不限"}` : "全部学期";
  $("#academic-range").textContent = range;
  $("#task-academic-summary").textContent = range + " · 在账户与安全中设置";
}

function updateAcademicControls() {
  const form = $("#academic-form"), custom = form.elements.academic_mode.value === "custom";
  form.elements.academic_start_date.disabled = !custom; form.elements.academic_start_date.required = custom;
  form.elements.academic_end_date.disabled = !custom;
}

function navigate(page) {
  const previousPage = state.page;
  state.page = page; location.hash = page;
  if (page === "today" && previousPage !== "today") state.cursor = new Date();
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === page));
  $$(".page").forEach((item) => item.classList.toggle("active", item.id === `page-${page}`));
  $("#add-button").hidden = !["today", "calendar"].includes(page);
  $("#previous-period").hidden = page !== "calendar"; $("#next-period").hidden = page !== "calendar"; $("#today-button").hidden = page !== "calendar";
  $("#show-completed").closest(".visibility-control").hidden = page !== "calendar";
  if (page === "today") renderToday();
  if (page === "calendar" && state.view === "week") { $("#calendar-canvas").scrollTop = state.weekScrollTop; $("#calendar-canvas").scrollLeft = state.weekScrollLeft; }
  updatePeriodTitle();
}

function updatePeriodTitle() {
  if (state.page === "today") { $("#period-title").textContent = "今日概览"; return; }
  if (state.page !== "calendar") { $("#period-title").textContent = ({ tasks: "待办", alerts: "提醒", connections: "连接", channels: "通知渠道", account: "账户" })[state.page] || "今天"; return; }
  if (state.view === "month") $("#period-title").textContent = fmtDate(state.cursor, { year: "numeric", month: "long" });
  else if (state.view === "week") { const a = startOfWeek(state.cursor), b = addDays(a,6); $("#period-title").textContent = `${fmtDate(a)}—${fmtDate(b)}`; }
  else $("#period-title").textContent = state.view === "agenda" ? "未来日程" : fmtDate(state.cursor, { year: "numeric", month: "long", day: "numeric", weekday: "long" });
}

function movePeriod(direction) {
  const d = new Date(state.cursor);
  if (state.page === "today") {
    d.setDate(d.getDate() + direction); state.cursor = d; renderToday(); updatePeriodTitle(); return;
  }
  if (state.view === "month") d.setMonth(d.getMonth() + direction); else d.setDate(d.getDate() + direction * (state.view === "week" ? 7 : 1)); state.cursor = d; renderCalendar(); updatePeriodTitle();
}

let syncPollTimer = null;
function scheduleSyncPoll() {
  if (syncPollTimer) return;
  syncPollTimer = setTimeout(async () => {
    syncPollTimer = null;
    if (!state.user) return;
    try {
      await loadDashboard();
      if (!state.data.sources.some((source) => source.syncing)) toast("同步已结束，请查看各来源状态");
    } catch (error) { if (error.status !== 401) { toast("进度读取失败，正在重试"); scheduleSyncPoll(); } }
  }, 2000);
}
async function syncSources(button, sourceId = null) {
  const selected = sourceId ? [sourceId] : state.data.sources.filter((source) => source.available !== false && source.enabled).map((source) => source.id);
  if (selected.some((id) => state.syncPending.has(id))) return;
  selected.forEach((id) => state.syncPending.add(id)); renderConnections();
  try {
    await api(sourceId ? `/api/sources/${encodeURIComponent(sourceId)}/sync` : "/api/sources/sync", { method: "POST" });
    await loadDashboard(); toast("已开始同步");
  } catch (error) { toast(error.status === 401 ? "请重新登录" : error.message); }
  finally { selected.forEach((id) => state.syncPending.delete(id)); renderConnections(); }
}

async function openSchoolDialog() {
  $("#school-error").textContent = ""; $("#captcha-field").hidden = true; if (state.school?.school_id) $("#school-form [name=username]").value = state.school.school_id; $("#school-dialog").showModal();
  try { const result = await api("/api/school/preload", { method: "POST" }); if (result.captcha_required) { $("#captcha-field").hidden = false; $("#captcha-image").src = `/api/school/captcha/${encodeURIComponent(result.captcha_id)}?t=${Date.now()}`; } } catch (e) { $("#school-error").textContent = e.message; }
}
async function disconnectSchool() { if (!confirm("断开北航账号？已同步的日程会保留。")) return; await api("/api/school/disconnect", { method: "POST" }); await schoolStateReset(); toast("已断开"); }
async function schoolStateReset() { const session = await api("/api/session"); state.school = session.school; await loadDashboard(); }

function channelOptions(selected = "in_app") {
  const options = [{ value: "in_app", label: "站内通知" }, ...state.data.channels.filter((c) => c.enabled).map((c) => ({ value: `${c.type}:${c.id}`, label: c.name }))];
  return options.map((item) => `<option value="${esc(item.value)}" ${item.value === selected ? "selected" : ""}>${esc(item.label)}</option>`).join("");
}
function addActionRow(action = { channel: "in_app", channel_id: null, after_minutes: 0 }) {
  const selected = action.channel_id ? `${action.channel}:${action.channel_id}` : action.channel;
  const row = document.createElement("div"); row.className = "action-row";
  const immediate = activeProfiles.has($("#rule-profile").value);
  row.classList.toggle("immediate-action", immediate);
  row.innerHTML = `${immediate ? '<span class="action-row-label">渠道</span>' : ''}<select data-action-channel aria-label="通知渠道">${channelOptions(selected)}</select><label ${immediate ? "hidden" : ""}>延后（分钟）<input data-action-delay type="number" min="0" max="10080" value="${immediate ? 0 : action.after_minutes || 0}"></label><button class="icon-button" type="button" aria-label="删除通知方式"><i class="bi bi-x"></i></button>`;
  row.querySelector("button").addEventListener("click", () => { if ($$("#action-list .action-row").length > 1) row.remove(); }); $("#action-list").append(row);
}
function profileDefaults(profile) {
  if (profile === "course") return {timing:"before_start",advance_minutes:15};
  if (deadlineProfiles.has(profile)) return { reminder_times: [{days:1,hours:0,minutes:0},{days:0,hours:2,minutes:0}], completion_filter: "incomplete" };
  if (profile === "course-change") return { change_types: Object.keys(courseChangeTypes) };
  if (profile === "spoc-assignment-grade") return {};
  if (changeProfiles.has(profile)) return { completion_filter: "incomplete" };
  return ({
    course: { before_start: 15 }, signin: { before_end: 30, repeat_every: 5, repeat_count: 6, completion_filter: "incomplete" },
    library: { before_start: 30, before_expiry: 15 }, assignment: { days_before: 1, hours_before: 2, completion_filter: "incomplete", assignment_type: "" },
    exam: { days_before: 7, hours_before: 24, final_hours: 2 }, grade: { poll_interval: 15 },
    fitness: { check_weekday: 1, check_hour: 9 },
    "boya-course": { before_start: 30 }, "boya-signout": { before_end: 20, repeat_every: 10, repeat_count: 2 },
    venue: { before_start: 60, before_end: 15 }, custom: { kind: "custom", anchor: "starts_at", offset: 15, direction: "before", source: "" },
  })[profile] || {};
}
function settingsFromRule(rule, profile) {
  if (deadlineProfiles.has(profile)) return {...profileDefaults(profile), ...(rule?.profile_settings || {}), reminder_times:deadlinePoints(rule)};
  if (profile === "course") {
    const saved = rule?.profile_settings || {}, trigger = (rule?.triggers?.length ? rule.triggers : [rule?.trigger]).filter(Boolean)[0];
    const timing = saved.timing || (trigger?.anchor === "ends_at" ? "before_end" : "before_start");
    return {timing, advance_minutes:saved.advance_minutes ?? saved[timing] ?? (trigger ? Math.abs(trigger.offset_minutes) : 15)};
  }
  if (rule?.profile_settings && Object.keys(rule.profile_settings).length) return { ...profileDefaults(profile), ...rule.profile_settings };
  const first = (rule?.triggers || [rule?.trigger]).filter(Boolean)[0];
  const defaults = profileDefaults(profile);
  if (profile === "course" && first) defaults.before_start = Math.abs(first.offset_minutes);
  if (profile === "signin" && first) defaults.before_end = Math.abs(first.offset_minutes);
  return defaults;
}
function renderProfileSettings(profile, settings = profileDefaults(profile)) {
  const n = (name, label, value, min = 0, max = 10080) => `<label class="rule-field"><span>${label}</span><input data-profile-setting="${name}" type="number" min="${min}" max="${max}" value="${esc(value)}" required></label>`;
  let html = "";
  if (profile === "course") html = `<label class="rule-field"><span>提醒时机</span><select data-profile-setting="timing"><option value="before_start" ${settings.timing === "before_start" ? "selected" : ""}>上课前</option><option value="before_end" ${settings.timing === "before_end" ? "selected" : ""}>课程结束前</option></select></label><div class="course-advance">${n("advance_minutes", "提前（分钟）", settings.advance_minutes)}</div>`;
  else if (profile === "course-change") html = `<div class="choice-grid change-type-grid">${Object.entries(courseChangeTypes).map(([value,label]) => `<label><input type="checkbox" data-change-type="${value}" ${(settings.change_types || []).includes(value) ? "checked" : ""}>${label}</label>`).join("")}</div>`;
  else if (changeProfiles.has(profile)) html = "";
  else if (profile === "signin") html = `<div class="form-row three">${n("before_end", "结束前检测（分钟）", settings.before_end)}${n("repeat_every", "未签到每隔（分钟）", settings.repeat_every, 0)}${n("repeat_count", "最多提醒（次）", settings.repeat_count, 1, 20)}</div>`;
  else if (profile === "library") html = `<div class="form-row">${n("before_start", "开始前（分钟）", settings.before_start)}${n("before_expiry", "离开失效前（分钟）", settings.before_expiry)}</div>`;
  else if (deadlineProfiles.has(profile)) html = `<div id="reminder-time-list">${settings.reminder_times.map(reminderTimeRow).join("")}</div><button type="button" class="inline-button" id="add-reminder-time"><i class="bi bi-plus"></i>添加提醒时间</button>`;
  else if (profile === "exam") html = `<div class="form-row three">${n("days_before", "首次（天前）", settings.days_before, 0, 365)}${n("hours_before", "再次（小时前）", settings.hours_before, 0, 720)}${n("final_hours", "最后（小时前）", settings.final_hours, 0, 72)}</div>`;
  else if (profile === "grade") html = `<div class="form-row">${n("poll_interval", "检查间隔（分钟）", settings.poll_interval, 5, 1440)}</div><div class="profile-fixed"><i class="bi bi-lightning-charge"></i><span>首次同步建立基线；之后新成绩出现或分数变化才提醒</span></div>`;
  else if (profile === "fitness") html = `<div class="form-row"><label>每周检查日<select data-profile-setting="check_weekday"><option value="1">周一</option><option value="2">周二</option><option value="3">周三</option><option value="4">周四</option><option value="5">周五</option><option value="6">周六</option><option value="7">周日</option></select></label>${n("check_hour", "检查时间（时）", settings.check_hour, 0, 23)}</div><div class="profile-fixed"><i class="bi bi-calculator"></i><span>仅当剩余次数达到“本周剩余额度 + 后续每周上限”时提醒</span></div>`;
  else if (profile === "boya-course") html = `<div class="form-row">${n("before_start", "上课前（分钟）", settings.before_start)}</div>`;
  else if (profile === "boya-signout") html = `<div class="form-row three">${n("before_end", "签退结束前（分钟）", settings.before_end)}${n("repeat_every", "未签退每隔（分钟）", settings.repeat_every, 0)}${n("repeat_count", "最多提醒（次）", settings.repeat_count, 1, 20)}</div>`;
  else if (profile === "venue") html = `<div class="form-row">${n("before_start", "预约开始前（分钟）", settings.before_start)}${n("before_end", "预约结束前（分钟）", settings.before_end)}</div>`;
  else html = `<div class="form-row three"><label>日程类型<select data-profile-setting="kind"><option value="custom">个人日程</option><option value="course">课程</option><option value="assignment">作业</option><option value="exam">考试</option><option value="booking">预约</option></select></label><label>基准时间<select data-profile-setting="anchor"><option value="starts_at">开始</option><option value="ends_at">结束</option><option value="due_at">截止</option></select></label>${n("offset", "相隔（分钟）", settings.offset)}</div><div class="form-row"><label>方向<select data-profile-setting="direction"><option value="before">之前</option><option value="after">之后</option></select></label><label>限定来源<input data-profile-setting="source" value="${esc(settings.source || "")}" placeholder="留空表示全部"></label></div>`;
  if (["signin", "assignment", "spoc-assignment", "judge-assignment", "spoc-new-assignment", "judge-new-assignment"].includes(profile)) {
    const choices = profile === "signin" ? [["incomplete", "仅未签到"], ["completed", "仅已签到"], ["unknown", "仅状态未知"], ["any", "全部状态"]] : [["incomplete", "未完成"], ["unsubmitted", "仅未提交"], ["partial", "仅部分提交"], ["submitted", "仅已提交"], ["unknown", "仅状态未知"], ["any", "全部状态"]];
    html += `<label class="rule-field completion-field"><span>提醒状态</span><select data-profile-setting="completion_filter">${choices.map(([value, label]) => `<option value="${value}" ${(settings.completion_filter || "incomplete") === value ? "selected" : ""}>${label}</option>`).join("")}</select></label>`;
  }
  $("#profile-settings").innerHTML = html;
  $("#rule-settings-fieldset").hidden = profile === "spoc-assignment-grade";
  Object.entries(settings).filter(([,value]) => !Array.isArray(value)).forEach(([key, value]) => { const input = $(`[data-profile-setting="${key}"]`); if (input) input.value = value; });
  $("#rule-settings-title").textContent = profile === "course-change" ? "变更类别" : deadlineProfiles.has(profile) ? "截止前提醒" : changeProfiles.has(profile) ? "检测设置" : "提醒时间";
}
function reminderTimeRow(point) {
  return `<div class="reminder-time-row"><span>截止前</span>${[["days","天",365],["hours","小时",23],["minutes","分钟",59]].map(([key,label,max]) => `<label><input type="number" data-advance="${key}" min="0" max="${max}" step="1" value="${esc(point[key] || 0)}" aria-label="提前${label}" required><span>${label}</span></label>`).join("")}<button type="button" class="icon-button" data-remove-reminder-time aria-label="删除提醒时间"><i class="bi bi-x"></i></button></div>`;
}
function openRuleDialog(rule = null) {
  if (rule?.available === false) { toast("Coming Soon"); return; }
  const form = $("#rule-form"); form.reset(); $("#action-list").innerHTML = ""; $("#rule-error").textContent = "";
  const editing = rule && Number.isInteger(rule.id); form.elements.rule_id.value = editing ? rule.id : ""; form.elements.rule_enabled.value = editing ? String(rule.enabled) : "true"; $("#rule-dialog-title").textContent = editing ? "编辑提醒规则" : "新建提醒规则"; $("#delete-rule-button").hidden = !editing;
  const profile = rule ? inferRuleProfile(rule) : "spoc-assignment";
  form.elements.connection.value = connectionForProfile(profile); renderRuleTypes(form.elements.connection.value, profile);
  if (rule) {
    form.elements.name.value = rule.name;
    const actions = activeProfiles.has(profile) ? [...new Map((rule.actions || []).map(a => [a.channel+":"+a.channel_id,a])).values()] : rule.actions || [];
    actions.forEach(addActionRow); form.elements.quiet_policy.value = rule.quiet_hours_policy || "delay";
  } else { form.elements.name.value = reminderProfiles[profile].name; addActionRow(); }
  renderProfileSettings(profile, settingsFromRule(rule, profile));
  if (!$("#action-list").children.length) addActionRow(); $("#rule-dialog").showModal();
}
function rulePayload(form) {
  const profile = form.elements.profile.value; const settings = {};
  $$('[data-profile-setting]').forEach((input) => { settings[input.dataset.profileSetting] = input.type === "number" ? Number(input.value) : input.value; });
  const actions = $$("#action-list .action-row").map((row) => { const [channel, id] = row.querySelector("[data-action-channel]").value.split(":"); return { channel, channel_id: id ? Number(id) : null, after_minutes: activeProfiles.has(profile) ? 0 : Number(row.querySelector("[data-action-delay]").value || 0) }; });
  if (new Set(actions.map(a => `${a.channel}:${a.channel_id}`)).size !== actions.length) throw new Error("通知方式不能重复");
  let scope = { kinds: [], sources: [] }, triggers = [], conditions = [], cancel_conditions = [];
  let repeat = { every_minutes: null, max_count: 1, until_anchor: null };
  if (profile === "course") { scope = { kinds: ["course"], sources: ["byxt"] }; triggers = [{ anchor: settings.timing === "before_end" ? "ends_at" : "starts_at", offset_minutes: -settings.advance_minutes }]; }
  else if (changeProfiles.has(profile)) {
    const source = connectionForProfile(profile);
    scope = {kinds:[source === "byxt" ? "course" : "assignment"],sources:[source]};
    if (profile === "course-change") {
      settings.change_types = $$('[data-change-type]:checked').map(input => input.dataset.changeType);
      if (!settings.change_types.length) throw new Error("至少选择一种课程变更类别");
    }
  }
  else if (profile === "signin") { scope = { kinds: ["signin"], sources: ["iclass"] }; triggers = [{ anchor: "ends_at", offset_minutes: -settings.before_end }]; conditions = []; cancel_conditions = []; repeat = { every_minutes: settings.repeat_every || null, max_count: settings.repeat_every ? settings.repeat_count : 1, until_anchor: "ends_at" }; }
  else if (profile === "library") { scope = { kinds: ["booking"], sources: ["libbook"] }; triggers = [{ anchor: "starts_at", offset_minutes: -settings.before_start }, { anchor: "ends_at", offset_minutes: -settings.before_expiry }]; conditions = [{ field: "status", op: "ne", value: "done" }]; cancel_conditions = [{ field: "status", op: "eq", value: "done" }]; }
  else if (deadlineProfiles.has(profile)) {
    settings.reminder_times = $$(".reminder-time-row").map(row => Object.fromEntries(["days","hours","minutes"].map(key => [key,Number(row.querySelector(`[data-advance="${key}"]`).value)])));
    if (!settings.reminder_times.length || settings.reminder_times.length > 10) throw new Error("请设置 1–10 个提醒时间点");
    const offsets = settings.reminder_times.map(advanceMinutes);
    if (new Set(offsets).size !== offsets.length) throw new Error("提醒时间点不能重复");
    scope = { kinds: ["assignment"], sources: profile === "spoc-assignment" ? ["spoc"] : profile === "judge-assignment" ? ["judge"] : ["spoc", "judge"] };
    triggers = offsets.map(minutes => ({anchor:"due_at",offset_minutes:-minutes}));
  }
  else if (profile === "exam") { scope = { kinds: ["exam"], sources: ["byxt"] }; triggers = [{ anchor: "starts_at", offset_minutes: -settings.days_before * 1440 }, { anchor: "starts_at", offset_minutes: -settings.hours_before * 60 }, { anchor: "starts_at", offset_minutes: -settings.final_hours * 60 }]; }
  else if (profile === "grade") { scope = { kinds: ["grade"], sources: ["grade"] }; triggers = [{ anchor: "starts_at", offset_minutes: 0 }]; conditions = [{ field: "status", op: "eq", value: "published" }]; }
  else if (profile === "fitness") { scope = { kinds: ["fitness"], sources: ["ygdk"] }; triggers = [{ anchor: "starts_at", offset_minutes: (Number(settings.check_weekday) - 1) * 1440 + Number(settings.check_hour) * 60 }]; conditions = [{ field: "metadata.must_max_every_week", op: "eq", value: true }]; cancel_conditions = [{ field: "status", op: "eq", value: "done" }]; repeat = { every_minutes: 10080, max_count: 40, until_anchor: "due_at" }; }
  else if (profile === "boya-course") { scope = { kinds: ["boya"], sources: ["bykc"] }; triggers = [{ anchor: "starts_at", offset_minutes: -settings.before_start }]; }
  else if (profile === "boya-signout") { scope = { kinds: ["signout"], sources: ["bykc"] }; triggers = [{ anchor: "ends_at", offset_minutes: -settings.before_end }]; conditions = [{ field: "status", op: "eq", value: "missing" }]; cancel_conditions = [{ field: "status", op: "eq", value: "done" }]; repeat = { every_minutes: settings.repeat_every || null, max_count: settings.repeat_every ? settings.repeat_count : 1, until_anchor: "ends_at" }; }
  else if (profile === "venue") { scope = { kinds: ["venue"], sources: ["cgyy"] }; triggers = [{ anchor: "starts_at", offset_minutes: -settings.before_start }, { anchor: "ends_at", offset_minutes: -settings.before_end }]; conditions = [{ field: "status", op: "in", value: ["upcoming", "pending", "confirmed"] }]; cancel_conditions = [{ field: "status", op: "in", value: ["cancelled", "rejected"] }]; }
  else { const direction = settings.direction === "before" ? -1 : 1; scope = { kinds: [settings.kind], sources: settings.source ? [settings.source] : [] }; triggers = [{ anchor: settings.anchor, offset_minutes: direction * settings.offset }]; }
  return { name: form.elements.name.value, enabled: form.elements.rule_enabled.value !== "false", profile, profile_settings: settings, scope, trigger: triggers[0] || null, triggers, conditions, cancel_conditions, repeat, actions, quiet_hours_policy: form.elements.quiet_policy.value };
}

const channelFieldSets = {
  email: `<div class="form-row"><label>SMTP 主机<input name="host" required></label><label>端口<input name="port" type="number" value="465" required></label></div><label>用户名<input name="username" required></label><label>密码或授权码<input name="password" type="password" required></label><div class="form-row"><label>发件地址<input name="from" type="email" required></label><label>收件地址<input name="to" type="email" required></label></div><label class="check-row"><input name="ssl" type="checkbox" checked><span>使用 SSL</span></label>`,
  sms_webhook: `<label>Webhook 地址<input name="url" type="url" required placeholder="https://..."></label><label>接收号码<input name="to"></label><p class="form-hint">POST JSON：title、message、to</p>`,
  wxpusher: `<label>AppToken<input name="app_token" type="password" required></label><label>UID<input name="uid" required></label>`,
  telegram: `<label>Bot Token<input name="bot_token" type="password" required></label><label>Chat ID<input name="chat_id" required></label>`,
};
function renderChannelFields() { $("#channel-fields").innerHTML = channelFieldSets[$("#channel-form [name=type]").value]; }

function bindEvents() {
  $$("[data-week-hour]").forEach((button) => button.addEventListener("click", () => { state.weekScrollTop = Number(button.dataset.weekHour) * WEEK_HOUR_HEIGHT; $("#calendar-canvas").scrollTop = state.weekScrollTop; }));
  $("#calendar-canvas").addEventListener("scroll", (event) => { if (state.view === "week" && state.page === "calendar") { state.weekScrollTop = event.currentTarget.scrollTop; state.weekScrollLeft = event.currentTarget.scrollLeft; } }, {passive:true});
  $$("[data-task-status]").forEach((button) => button.addEventListener("click", () => { state.taskStatus = button.dataset.taskStatus; renderTasks(); }));
  $("#task-source").addEventListener("change", (event) => { state.taskSource = event.target.value; state.taskType = ""; renderTasks(); });
  $("#task-type").addEventListener("change", (event) => { state.taskType = event.target.value; renderTasks(); });
  $("#task-history").addEventListener("change", (event) => { state.taskHistory = event.target.checked; renderTasks(); });
  $("#show-completed").addEventListener("change", (event) => { state.showCompleted = event.target.checked; renderToday(); renderCalendar(); });
  $("#academic-form [name=academic_mode]").addEventListener("change", updateAcademicControls);
  $("#academic-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget, custom = form.elements.academic_mode.value === "custom";
    try {
      await api("/api/preferences", { method: "PUT", body: JSON.stringify({
        academic_filter_enabled: form.elements.academic_filter_enabled.checked,
        academic_start_date: custom ? form.elements.academic_start_date.value : null,
        academic_end_date: custom ? (form.elements.academic_end_date.value || null) : null
      }) });
      document.activeElement?.blur(); await loadDashboard(); toast("学期范围已保存");
    } catch (error) { toast(error.message); }
  });
  $$("[data-auth-mode]").forEach((button) => button.addEventListener("click", () => { state.authMode = button.dataset.authMode; $$("[data-auth-mode]").forEach((b) => b.classList.toggle("active", b === button)); $$(".register-only").forEach((el) => el.hidden = state.authMode !== "register"); $("#account-form [name=display_name]").required = state.authMode === "register"; $("#account-submit-label").textContent = state.authMode === "register" ? "创建账户" : "登录"; $("#account-form [name=password]").autocomplete = state.authMode === "register" ? "new-password" : "current-password"; $("#account-error").textContent = ""; }));
  $("#account-form").addEventListener("submit", async (event) => { event.preventDefault(); const formElement = event.currentTarget; const values = Object.fromEntries(new FormData(formElement)); if (state.authMode === "login") delete values.display_name; try { const result = await api(`/api/account/${state.authMode}`, { method: "POST", body: JSON.stringify(values) }); state.user = result.user; formElement.reset(); showApp(); await loadDashboard(); navigate("today"); } catch (e) { $("#account-error").textContent = ({ invalid_credentials: "邮箱或密码错误", email_exists: "该邮箱已注册" })[e.message] || e.message; } });
  $$(".nav-item").forEach((item) => item.addEventListener("click", (event) => { event.preventDefault(); navigate(item.dataset.page); }));
  $("#user-button").addEventListener("click", () => navigate("account"));
  $$('[data-view]').forEach((button) => button.addEventListener("click", () => { state.view = button.dataset.view; renderCalendar(); updatePeriodTitle(); }));
  $("#previous-period").addEventListener("click", () => movePeriod(-1)); $("#next-period").addEventListener("click", () => movePeriod(1)); $("#today-button").addEventListener("click", () => { state.cursor = new Date(); if (state.page === "today") renderToday(); else renderCalendar(); updatePeriodTitle(); });
  $("#add-button").addEventListener("click", () => $("#event-dialog").showModal());
  $$('[data-close]').forEach((button) => button.addEventListener("click", () => $(`#${button.dataset.close}`).close()));
  $("#event-form").addEventListener("submit", async (event) => { event.preventDefault(); const formElement = event.currentTarget; const formData = new FormData(formElement); const values = Object.fromEntries(formData); ["starts_at","ends_at","due_at"].forEach((key) => { if (values[key]) values[key] = new Date(values[key]).toISOString(); else delete values[key]; }); values.all_day = formData.has("all_day"); if (!values.location) delete values.location; try { await api("/api/events", { method: "POST", body: JSON.stringify(values) }); $("#event-dialog").close(); formElement.reset(); await loadDashboard(); toast("日程已保存"); } catch { toast("保存失败"); } });
  $("#school-form").addEventListener("submit", async (event) => { event.preventDefault(); const formElement = event.currentTarget; const formData = new FormData(formElement); const values = Object.fromEntries(formData); values.remember_password = formData.has("remember_password"); if (!values.captcha) delete values.captcha; try { const result = await api("/api/school/login", { method: "POST", body: JSON.stringify(values) }); if (result.captcha_required) { $("#captcha-field").hidden = false; $("#captcha-image").src = `/api/school/captcha/${encodeURIComponent(result.captcha_id)}?t=${Date.now()}`; return; } $("#school-dialog").close(); formElement.reset(); await loadDashboard(); toast(result.school?.status === "connected" ? "北航账号已连接" : (result.school?.detail || "教务授权未完成")); } catch (e) { $("#school-error").textContent = e.message; } });
  $$('[data-alert-tab]').forEach((button) => button.addEventListener("click", () => { $$('[data-alert-tab]').forEach((item) => item.classList.toggle("active", item === button)); $$('[data-alert-panel]').forEach((panel) => panel.classList.toggle("active", panel.dataset.alertPanel === button.dataset.alertTab)); }));
  $("#add-rule-button").addEventListener("click", () => openRuleDialog()); $("#add-action").addEventListener("click", () => addActionRow());
  $("#rule-connection").addEventListener("change", (event) => { renderRuleTypes(event.currentTarget.value); const profile = $("#rule-profile").value; renderProfileSettings(profile); $("#rule-form [name=name]").value = reminderProfiles[profile].name; });
  $("#rule-profile").addEventListener("change", (event) => { const profile = event.currentTarget.value; renderProfileSettings(profile); $("#rule-form [name=name]").value = reminderProfiles[profile].name; });
  $("#profile-settings").addEventListener("click", event => {
    if (event.target.closest("#add-reminder-time")) {
      if ($$(".reminder-time-row").length >= 10) { toast("最多 10 个时间点"); return; }
      $("#reminder-time-list").insertAdjacentHTML("beforeend", reminderTimeRow({days:0,hours:0,minutes:30}));
    }
    const remove = event.target.closest("[data-remove-reminder-time]");
    if (remove) { if ($$(".reminder-time-row").length > 1) remove.closest(".reminder-time-row").remove(); else toast("至少保留一个提醒时间"); }
  });
  $("#rule-form").addEventListener("submit", async (event) => { event.preventDefault(); const formElement = event.currentTarget; const id = formElement.elements.rule_id.value; try { const payload = rulePayload(formElement); await api(id ? `/api/rules/${id}` : "/api/rules", { method: id ? "PUT" : "POST", body: JSON.stringify(payload) }); if (payload.profile === "grade" && state.data.sources.some((source) => source.id === "grade")) await api("/api/sources/grade", { method: "PATCH", body: JSON.stringify({ refresh_interval_minutes: Number(payload.profile_settings.poll_interval) }) }); $("#rule-dialog").close(); await loadDashboard(); toast("规则已保存"); } catch (e) { $("#rule-error").textContent = e.message; } });
  $("#delete-rule-button").addEventListener("click", async () => { const id = $("#rule-form").elements.rule_id.value; if (!id || !confirm("删除这条提醒规则？未发送的计划也会取消。")) return; await api(`/api/rules/${id}`, { method: "DELETE" }); $("#rule-dialog").close(); await loadDashboard(); toast("规则已删除"); });
  $("#add-channel-button").addEventListener("click", () => { $("#channel-form").reset(); $("#channel-error").textContent = ""; renderChannelFields(); $("#channel-dialog").showModal(); }); $("#channel-form [name=type]").addEventListener("change", renderChannelFields); renderChannelFields();
  $("#channel-form").addEventListener("submit", async (event) => { event.preventDefault(); const formElement = event.currentTarget; const data = Object.fromEntries(new FormData(formElement)); const type = data.type, name = data.name; delete data.type; delete data.name; if ("ssl" in data) data.ssl = true; try { await api("/api/channels", { method: "POST", body: JSON.stringify({ type, name, enabled: true, config: data }) }); $("#channel-dialog").close(); formElement.reset(); await loadDashboard(); toast("渠道已保存"); } catch (e) { $("#channel-error").textContent = e.message; } });
  $("#preferences-form").addEventListener("submit", async (event) => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); await api("/api/preferences", { method: "PUT", body: JSON.stringify({ timezone: "Asia/Shanghai", quiet_start: data.quiet_start || null, quiet_end: data.quiet_end || null }) }); await loadDashboard(); toast("安静时段已保存"); });
  $("#sync-button").addEventListener("click", (e) => syncSources(e.currentTarget)); $("#source-sync").addEventListener("click", (e) => syncSources(e.currentTarget));
  $("#logout-button").addEventListener("click", async () => { await api("/api/account/logout", { method: "POST" }); state.user = null; showAuth(); });
}

document.addEventListener("DOMContentLoaded", () => { bindEvents(); bootstrap(); });
