from __future__ import annotations

import hmac
import json
import sqlite3
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import Response as RawResponse
from fastapi.staticfiles import StaticFiles

from app.adapters import (
    BykcAdapter, CourseReminderAdapter, CgyyAdapter, GradeAdapter, IClassAdapter, JudgeAdapter, LibBookAdapter, SpocAdapter,
    YgdkAdapter,
)
from app.config import ROOT, settings
from app.db import Database, now_iso
from app.features import ACTIVE_SOURCES, event_available, public_rule, rule_available
from app.models import (
    AutomationRuleCreate, AutomationRuleUpdate, EventCreate, EventIgnoreUpdate, NotificationChannelCreate,
    NotificationChannelUpdate, PreferencesUpdate, RegisterRequest, SchoolLoginRequest, SourceUpdate,
    UserLoginRequest,
)
from app.security import CredentialVault, hash_password, new_token, token_digest, verify_password
from app.services.aggregator import Aggregator
from app.services.academic import academic_window, in_academic_window
from app.services.event_state import public_event
from app.services.notifications import NotificationSender, TriggerDispatcher, validate_channel_config
from app.services.rules import RULE_TEMPLATES, RuleEngine
from app.services.school_sessions import SchoolSessionManager
from app.services.source_health import source_transition_message
from app.services.worker import BackgroundWorker
from app.upstream.sso import LoginFailed, AuthenticationRequired


SESSION_COOKIE = "puaa_session"
CSRF_COOKIE = "puaa_csrf"
db = Database(settings.database_path)
vault = CredentialVault(settings.database_path.parent, settings.master_key)
school_sessions = SchoolSessionManager(settings.trust_env_proxy)
rule_engine = RuleEngine(db, enforce_feature_scope=True)
notification_sender = NotificationSender(db, vault)
dispatcher = TriggerDispatcher(db, notification_sender, enforce_feature_scope=True)
worker: BackgroundWorker | None = None
sync_locks: dict[int, asyncio.Lock] = {}
sync_jobs: dict[tuple[int, str], asyncio.Task] = {}
sync_progress: dict[tuple[int, str], dict[str, Any]] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    global worker
    db.init()
    # Apply the current scope before dispatching any previously queued work.
    for row in db._rows("SELECT id FROM puaa_users WHERE disabled_at IS NULL"):
        rule_engine.plan_user(row["id"])
    worker = BackgroundWorker(
        dispatcher,
        sync_all_remembered,
        # Notification dispatch still runs every 15 seconds.  Source-level
        # intervals are evaluated once a minute by sync_all_remembered.
        sync_interval=60,
    )
    worker.start()
    yield
    if worker:
        await worker.stop()
    pending = [task for task in sync_jobs.values() if not task.done()]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    sync_jobs.clear()
    sync_progress.clear()
    await school_sessions.close()


app = FastAPI(title="PUAA", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def fresh_local_content(request: Request, call_next):
    response = await call_next(request)
    # Private API responses and development assets must not remain stale after
    # a local restart, especially when old scripts use a different contract.
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
    return response


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {"id": user["id"], "email": user["email"], "display_name": user["display_name"]}


def current_user(request: Request) -> dict[str, Any]:
    token = request.cookies.get(SESSION_COOKIE)
    user = db.session_user(token_digest(token)) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="not_authenticated")
    return user


def csrf_user(
    request: Request,
    x_csrf_token: str | None = Header(default=None),
) -> dict[str, Any]:
    user = current_user(request)
    if not x_csrf_token or not hmac.compare_digest(user["csrf_hash"], token_digest(x_csrf_token)):
        raise HTTPException(status_code=403, detail="csrf_failed")
    return user


def set_session(response: Response, user_id: int) -> str:
    token, csrf = new_token(), new_token()
    db.create_session(user_id, token_digest(token), token_digest(csrf), settings.session_days)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_days * 86400,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=settings.session_days * 86400,
        httponly=False,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )
    return csrf


async def sync_for(user_id: int, source_ids: set[str] | None = None) -> dict[str, int]:
    selected = (source_ids or ACTIVE_SOURCES) & ACTIVE_SOURCES
    tasks = start_sync(user_id, selected)
    results = await asyncio.gather(*tasks.values())
    return {source: count for source, count in zip(tasks, results)}


def start_sync(user_id: int, source_ids: set[str]) -> dict[str, asyncio.Task]:
    tasks = {}
    for source_id in sorted(source_ids & ACTIVE_SOURCES):
        if not (db.get_source(user_id, source_id) or {}).get("enabled", True):
            continue
        key = (user_id, source_id)
        task = sync_jobs.get(key)
        if task is None or task.done():
            sync_progress[key] = {"completed": 0, "total": 0, "phase": "授权中"}
            task = asyncio.create_task(sync_one(user_id, source_id), name=f"sync-{user_id}-{source_id}")
            sync_jobs[key] = task
        tasks[source_id] = task
    return tasks


async def sync_one(user_id: int, source_id: str) -> int:
    # Enforce the boundary even for internal/direct calls, before SSO access.
    if source_id not in ACTIVE_SOURCES:
        return 0
    key = (user_id, source_id)
    previous = db.get_source(user_id, source_id)
    lock = sync_locks.setdefault(user_id, asyncio.Lock())
    try:
        vpn_mode = source_id == "judge" and (previous or {}).get("network_mode") == "webvpn"
        school = await school_sessions.get_webvpn(user_id) if vpn_mode else await school_sessions.get(user_id)
        # Only the common SSO login needs serialization; slow business systems
        # must not block the other sources or notification dispatch.
        async with lock:
            if not school.authenticated:
                secret = db.school_secret(user_id)
                if secret and secret.get("remember_password") and secret.get("password_ciphertext"):
                    await school.login(secret["school_id"], vault.decrypt(user_id, secret["password_ciphertext"]))
                if not school.authenticated:
                    if vpn_mode:
                        raise AuthenticationRequired("学校 WebVPN 需要登录，请在 JUDGE 连接中选择登录")
                    db.upsert_school_connection(user_id, {"status": "login_required", "detail": "统一认证会话已失效"})
        if source_id == "judge":
            adapter = JudgeAdapter(school, previous_events=db.list_events(user_id, limit=5000),
                                   academic_window=academic_window(db.preferences(user_id)))
        else:
            adapter = {"spoc": SpocAdapter, "byxt": CourseReminderAdapter}[source_id](school)
        adapter.on_progress = lambda values: sync_progress.__setitem__(key, values)
        sync_progress[key]["phase"] = "读取数据"
        aggregator = Aggregator(db, user_id, [adapter])
        result = await aggregator.sync()
        current_sources = db.list_sources(user_id)
        current = db.get_source(user_id, source_id)
        if vpn_mode and current and current["status"] == "login_required":
            school.user = None  # Next refresh can reauthorize with explicitly saved credentials.
        message = source_transition_message(previous, current) if current else None
        if message:
            await notification_sender.send(user_id, "in_app", None, message[0], message[1])
        if school.authenticated:
            partial = [item for item in current_sources if item["id"] in ACTIVE_SOURCES
                       and item.get("enabled", True) and item["status"] != "healthy"]
            db.upsert_school_connection(user_id, {
                "status": "degraded" if partial else "connected",
                "last_sync_at": now_iso(),
                "detail": f"{len(partial)} 个来源异常" if partial else "",
            })
        rule_engine.plan_user(user_id)
        return result.get(source_id, 0)
    except asyncio.CancelledError:
        raise
    except AuthenticationRequired as exc:
        label = {"spoc": SpocAdapter, "byxt": CourseReminderAdapter, "judge": JudgeAdapter}[source_id].label
        db.upsert_source(user_id, {"id": source_id, "label": label, "status": "login_required",
            "detail": str(exc), "last_sync_at": now_iso(), "event_count": (previous or {}).get("event_count", 0)})
        return 0
    except Exception as exc:
        label = {"spoc": SpocAdapter, "byxt": CourseReminderAdapter, "judge": JudgeAdapter}[source_id].label
        db.upsert_source(user_id, {"id": source_id, "label": label, "status": "error",
            "detail": Aggregator._error_detail(exc), "last_sync_at": now_iso(),
            "event_count": (previous or {}).get("event_count", 0)})
        return 0
    finally:
        sync_progress.pop(key, None)


async def sync_all_remembered() -> None:
    for user_id in db.list_auto_sync_user_ids():
        due = set(db.due_source_ids(user_id)) & ACTIVE_SOURCES
        if due:
            start_sync(user_id, due)


@app.get("/api/health")
def health():
    return {"status": "ok", "demo_data": False, "multi_user": True}


@app.get("/api/connectors")
def connectors(_: dict[str, Any] = Depends(current_user)):
    return [{**item, "available": item["id"] in ACTIVE_SOURCES,
             "availability": "available" if item["id"] in ACTIVE_SOURCES else "coming_soon"}
            for item in [
        CourseReminderAdapter.manifest.public(),
        IClassAdapter.manifest.public(),
        GradeAdapter.manifest.public(),
        SpocAdapter.manifest.public(),
        JudgeAdapter.manifest.public(),
        BykcAdapter.manifest.public(),
        CgyyAdapter.manifest.public(),
        YgdkAdapter.manifest.public(),
        LibBookAdapter.manifest.public(),
    ]]


@app.post("/api/account/register", status_code=201)
def register(payload: RegisterRequest, response: Response):
    try:
        user = db.create_user(payload.email, payload.display_name, hash_password(payload.password))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="email_exists") from exc
    csrf = set_session(response, user["id"])
    return {"user": public_user(user), "csrf_token": csrf}


@app.post("/api/account/login")
def login(payload: UserLoginRequest, response: Response):
    user = db.get_user_by_email(payload.email)
    if not user or not verify_password(user["password_hash"], payload.password):
        raise HTTPException(status_code=401, detail="invalid_credentials")
    csrf = set_session(response, user["id"])
    return {"user": public_user(user), "csrf_token": csrf}


@app.get("/api/session")
def session(user: dict[str, Any] = Depends(current_user)):
    return {"user": public_user(user), "school": db.school_connection(user["id"])}


@app.post("/api/account/logout", status_code=204)
async def logout(request: Request, response: Response, user: dict[str, Any] = Depends(csrf_user)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.delete_session(token_digest(token))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return None


@app.post("/api/school/preload")
async def school_preload(network_mode: Literal["direct", "webvpn"] = "direct", user: dict[str, Any] = Depends(csrf_user)):
    try:
        upstream = await school_sessions.get_webvpn(user["id"]) if network_mode == "webvpn" else await school_sessions.get(user["id"])
        return await upstream.preload()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="学校 WebVPN 暂不可达" if network_mode == "webvpn" else "北航统一认证暂不可达") from exc


@app.get("/api/school/captcha/{captcha_id}")
async def school_captcha(captcha_id: str, network_mode: Literal["direct", "webvpn"] = "direct", user: dict[str, Any] = Depends(current_user)):
    try:
        upstream = await school_sessions.get_webvpn(user["id"]) if network_mode == "webvpn" else await school_sessions.get(user["id"])
        content, media_type = await upstream.captcha(captcha_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="验证码获取失败") from exc
    return RawResponse(content=content, media_type=media_type)


@app.post("/api/school/login")
async def school_login(payload: SchoolLoginRequest, user: dict[str, Any] = Depends(csrf_user)):
    vpn_mode = payload.network_mode == "webvpn"
    existing = db.school_secret(user["id"])
    if vpn_mode:
        if not existing or payload.username != existing["school_id"]:
            raise HTTPException(status_code=422, detail="WebVPN 请使用已连接的学校账号")
        if (db.get_source(user["id"], "judge") or {}).get("network_mode") != "webvpn":
            raise HTTPException(status_code=409, detail="请先选择 JUDGE 的 WebVPN 模式")
    upstream = await school_sessions.get_webvpn(user["id"]) if vpn_mode else await school_sessions.get(user["id"])
    try:
        status = await upstream.login(payload.username, payload.password, payload.captcha)
    except LoginFailed as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="学校 WebVPN 暂不可达" if vpn_mode else "北航统一认证暂不可达") from exc
    if not status.get("authenticated"):
        return status
    profile = status.get("user") or {}
    if vpn_mode:
        if payload.remember_password:
            db.upsert_school_connection(user["id"], {
                "remember_password": True, "password_ciphertext": vault.encrypt(user["id"], payload.password)})
        start_sync(user["id"], {"judge"})
        return {"authenticated": True, "network_mode": "webvpn", "school": db.school_connection(user["id"])}
    encrypted = vault.encrypt(user["id"], payload.password) if payload.remember_password else None
    db.upsert_school_connection(user["id"], {
        "school_id": payload.username,
        "display_name": profile.get("name") or profile.get("userName"),
        "status": "connected",
        "remember_password": payload.remember_password,
        "password_ciphertext": encrypted,
        "last_login_at": now_iso(),
        "detail": "",
    })
    if existing and existing["school_id"] != payload.username:
        await school_sessions.reset_webvpn(user["id"])
    if (db.get_source(user["id"], "judge") or {}).get("network_mode") == "webvpn":
        # Reuse only this explicit login's password, never retain it in memory.
        # A separate CAPTCHA must be solved by the user; do not reuse the SSO code.
        try:
            vpn = await school_sessions.get_webvpn(user["id"])
            await vpn.login(payload.username, payload.password)
        except Exception:
            pass  # Primary SSO login remains valid; the JUDGE source reports its own error.
    start_sync(user["id"], set(ACTIVE_SOURCES))
    return {"authenticated": True, "school": db.school_connection(user["id"])}


@app.post("/api/school/disconnect", status_code=204)
async def school_disconnect(user: dict[str, Any] = Depends(csrf_user)):
    tasks = [task for (uid, _), task in sync_jobs.items() if uid == user["id"] and not task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await school_sessions.remove(user["id"])
    db.disconnect_school(user["id"])
    return None


@app.get("/api/dashboard")
def dashboard(user: dict[str, Any] = Depends(current_user)):
    data = db.dashboard(user["id"])
    data["academic_window"] = academic_window(data["preferences"])
    data["events"] = [public_event(event) for event in data["events"]
                      if event_available(event) and in_academic_window(event, data["academic_window"])]
    data["rules"] = [public_rule(rule) for rule in data["rules"]]
    existing = {source["id"]: source for source in data["sources"]}
    data["sources"] = []
    for manifest in connectors(user):
        source = existing.get(manifest["id"], {
            "id": manifest["id"], "label": manifest["label"], "enabled": True,
            "status": "not_connected", "detail": "未连接", "last_sync_at": None,
            "event_count": 0, "refresh_interval_minutes": 60,
            "network_mode": "direct",
        })
        available = manifest["available"]
        data["sources"].append({**source, "available": available,
            "webvpn_login_required": bool(manifest["id"] == "judge" and source.get("network_mode") == "webvpn"
                and not school_sessions.webvpn_authenticated(user["id"])) or bool(manifest["id"] == "judge"
                and source.get("network_mode") == "webvpn" and source.get("status") == "login_required"),
            "syncing": bool(sync_jobs.get((user["id"], manifest["id"])) and not sync_jobs[(user["id"], manifest["id"])].done()),
            "sync_progress": sync_progress.get((user["id"], manifest["id"])),
            "label": manifest["label"],
            "availability": manifest["availability"],
            **({} if available else {"enabled": False, "status": "coming_soon", "detail": "Coming Soon"})})
    return data


@app.get("/api/events")
def events(
    start: str | None = None,
    end: str | None = None,
    user: dict[str, Any] = Depends(current_user),
):
    window = academic_window(db.preferences(user["id"]))
    return [public_event(event) for event in db.list_events(user["id"], start=start, end=end)
            if event_available(event) and in_academic_window(event, window)]


@app.post("/api/events", status_code=201)
def create_event(payload: EventCreate, user: dict[str, Any] = Depends(csrf_user)):
    require_available(False)
    event = db.create_custom_event(user["id"], payload.model_dump())
    rule_engine.plan_user(user["id"])
    return event


@app.patch("/api/events/{event_id}/ignore")
def ignore_event(event_id: int, payload: EventIgnoreUpdate, user: dict[str, Any] = Depends(csrf_user)):
    event = db.event_by_id(user["id"], event_id)
    if not event:
        raise HTTPException(status_code=404, detail="event_not_found")
    require_available(event_available(event))
    updated = db.set_event_ignored(user["id"], event_id, payload.ignored)
    rule_engine.plan_user(user["id"])
    db.audit(user["id"], "ignore" if payload.ignored else "unignore", "event", str(event_id))
    return public_event(updated)


def require_available(available: bool) -> None:
    if not available:
        raise HTTPException(status_code=403, detail="Coming Soon")


@app.post("/api/sources/sync", status_code=202)
async def sync_sources(user: dict[str, Any] = Depends(csrf_user)):
    return {"started": list(start_sync(user["id"], set(ACTIVE_SOURCES)))}


@app.post("/api/sources/{source_id}/sync", status_code=202)
async def sync_source(source_id: str, user: dict[str, Any] = Depends(csrf_user)):
    require_available(source_id in ACTIVE_SOURCES)
    if not (db.get_source(user["id"], source_id) or {}).get("enabled", True):
        raise HTTPException(status_code=409, detail="请先启用此来源")
    return {"started": list(start_sync(user["id"], {source_id}))}


@app.patch("/api/sources/{source_id}")
def update_source(source_id: str, payload: SourceUpdate, user: dict[str, Any] = Depends(csrf_user)):
    if source_id not in ACTIVE_SOURCES:
        raise HTTPException(status_code=403, detail="Coming Soon")
    if payload.network_mode is not None:
        if source_id != "judge":
            raise HTTPException(status_code=422, detail="目前仅 JUDGE 支持 WebVPN 模式")
        running = sync_jobs.get((user["id"], source_id))
        if running and not running.done():
            raise HTTPException(status_code=409, detail="请等待当前同步完成后切换访问模式")
        if db.get_source(user["id"], source_id) is None:
            db.upsert_source(user["id"], {"id": source_id, "label": JudgeAdapter.label, "status": "not_connected"})
    source = db.update_source_settings(
        user["id"], source_id,
        enabled=payload.enabled,
        refresh_interval_minutes=payload.refresh_interval_minutes,
        network_mode=payload.network_mode,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="source_not_found")
    if payload.enabled:
        rule_engine.plan_user(user["id"])
    db.audit(user["id"], "update", "source", source_id, payload.model_dump(exclude_none=True))
    return source


@app.get("/api/rule-templates")
def rule_templates(_: dict[str, Any] = Depends(current_user)):
    return [{**template, "available": rule_available(template),
             "availability": "available" if rule_available(template) else "coming_soon"}
            for template in RULE_TEMPLATES]


@app.get("/api/rules")
def rules(user: dict[str, Any] = Depends(current_user)):
    return [public_rule(rule) for rule in db.list_automation_rules(user["id"])]


@app.post("/api/rules", status_code=201)
def create_rule(payload: AutomationRuleCreate, user: dict[str, Any] = Depends(csrf_user)):
    if not rule_available(payload.model_dump()):
        raise HTTPException(status_code=403, detail="Coming Soon")
    rule = db.create_automation_rule(user["id"], payload.model_dump())
    rule_engine.plan_user(user["id"])
    db.audit(user["id"], "create", "rule", str(rule["id"]))
    return rule


@app.put("/api/rules/{rule_id}")
def replace_rule(rule_id: int, payload: AutomationRuleCreate, user: dict[str, Any] = Depends(csrf_user)):
    current = db.get_automation_rule(user["id"], rule_id)
    if current and (not rule_available(current) or not rule_available(payload.model_dump())):
        raise HTTPException(status_code=403, detail="Coming Soon")
    rule = db.update_automation_rule(user["id"], rule_id, payload.model_dump())
    if rule is None:
        raise HTTPException(status_code=404, detail="rule_not_found")
    rule_engine.plan_user(user["id"])
    db.audit(user["id"], "update", "rule", str(rule_id))
    return rule


@app.patch("/api/rules/{rule_id}")
def update_rule(rule_id: int, payload: AutomationRuleUpdate, user: dict[str, Any] = Depends(csrf_user)):
    current = db.get_automation_rule(user["id"], rule_id)
    if current and (not rule_available(current) or not rule_available({
        **current, **payload.model_dump(exclude_unset=True)
    })):
        raise HTTPException(status_code=403, detail="Coming Soon")
    if current:
        # PATCH must enforce the same timing constraints as create/replace;
        # otherwise a client could bypass the minimum advance by editing triggers.
        merged = {**current, **payload.model_dump(exclude_unset=True)}
        try:
            validated = AutomationRuleCreate.model_validate(merged)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        values = validated.model_dump()
    else:
        values = payload.model_dump(exclude_unset=True)
    rule = db.update_automation_rule(user["id"], rule_id, values)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule_not_found")
    rule_engine.plan_user(user["id"])
    return rule


@app.delete("/api/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int, user: dict[str, Any] = Depends(csrf_user)):
    if not db.delete_automation_rule(user["id"], rule_id):
        raise HTTPException(status_code=404, detail="rule_not_found")
    db.audit(user["id"], "delete", "rule", str(rule_id))


@app.get("/api/triggers")
def triggers(user: dict[str, Any] = Depends(current_user)):
    return db.list_triggers(user["id"])


@app.get("/api/channels")
def channels(user: dict[str, Any] = Depends(current_user)):
    return db.list_channels(user["id"])


@app.post("/api/channels", status_code=201)
def create_channel(payload: NotificationChannelCreate, user: dict[str, Any] = Depends(csrf_user)):
    try:
        validate_channel_config(payload.type, payload.config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    ciphertext = vault.encrypt(user["id"], json.dumps(payload.config, ensure_ascii=False))
    channel = db.create_channel(user["id"], payload.type, payload.name, ciphertext, payload.enabled)
    db.audit(user["id"], "create", "channel", str(channel["id"]), {"type": payload.type})
    return channel


@app.patch("/api/channels/{channel_id}")
def update_channel(channel_id: int, payload: NotificationChannelUpdate, user: dict[str, Any] = Depends(csrf_user)):
    values = payload.model_dump(exclude_unset=True)
    current = db.get_channel(user["id"], channel_id, include_secret=True)
    if not current:
        raise HTTPException(status_code=404, detail="channel_not_found")
    if payload.config is not None:
        try:
            validate_channel_config(current["type"], payload.config)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        values["config_ciphertext"] = vault.encrypt(user["id"], json.dumps(payload.config, ensure_ascii=False))
        values.pop("config", None)
    return db.update_channel(user["id"], channel_id, values)


@app.delete("/api/channels/{channel_id}", status_code=204)
def delete_channel(channel_id: int, user: dict[str, Any] = Depends(csrf_user)):
    if not db.delete_channel(user["id"], channel_id):
        raise HTTPException(status_code=404, detail="channel_not_found")
    db.audit(user["id"], "delete", "channel", str(channel_id))


@app.post("/api/channels/{channel_id}/test")
async def test_channel(channel_id: int, user: dict[str, Any] = Depends(csrf_user)):
    channel = db.get_channel(user["id"], channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="channel_not_found")
    try:
        await notification_sender.send(user["id"], channel["type"], channel_id, "PUAA 测试通知", "通知渠道配置成功")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "sent"}


@app.post("/api/channels/test-in-app")
async def test_in_app(user: dict[str, Any] = Depends(csrf_user)):
    await notification_sender.send(user["id"], "in_app", None, "PUAA 测试通知", "站内通知工作正常")
    return {"status": "sent"}


@app.get("/api/deliveries")
def deliveries(user: dict[str, Any] = Depends(current_user)):
    return db.list_delivery_v2(user["id"])


@app.get("/api/inbox")
def inbox(user: dict[str, Any] = Depends(current_user)):
    return db.list_inbox(user["id"])


@app.put("/api/preferences")
def preferences(payload: PreferencesUpdate, user: dict[str, Any] = Depends(csrf_user)):
    result = db.update_preferences(user["id"], payload.model_dump(mode="json", exclude_unset=True))
    rule_engine.plan_user(user["id"])
    return result


web_root = Path(ROOT / "web")
app.mount("/", StaticFiles(directory=web_root, html=True), name="web")
