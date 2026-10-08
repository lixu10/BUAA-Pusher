from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    """Small SQLite repository with explicit tenant scoping on every business query."""

    def __init__(self, path: Path | str):
        self.path = Path(path) if str(path) != ":memory:" else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __del__(self) -> None:
        try:
            self._connection.close()
        except Exception:
            pass

    def init(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS puaa_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    disabled_at TEXT
                );
                CREATE TABLE IF NOT EXISTS puaa_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES puaa_users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    csrf_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS school_connections (
                    user_id INTEGER PRIMARY KEY REFERENCES puaa_users(id) ON DELETE CASCADE,
                    school_id TEXT NOT NULL,
                    display_name TEXT,
                    status TEXT NOT NULL DEFAULT 'login_required',
                    remember_password INTEGER NOT NULL DEFAULT 0,
                    password_ciphertext TEXT,
                    last_login_at TEXT,
                    last_sync_at TEXT,
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS calendar_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES puaa_users(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    starts_at TEXT,
                    ends_at TEXT,
                    due_at TEXT,
                    all_day INTEGER NOT NULL DEFAULT 0,
                    location TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, source, external_id)
                );
                CREATE INDEX IF NOT EXISTS idx_calendar_events_user_time
                    ON calendar_events(user_id, starts_at, due_at);
                CREATE TABLE IF NOT EXISTS user_sources (
                    user_id INTEGER NOT NULL REFERENCES puaa_users(id) ON DELETE CASCADE,
                    id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    last_sync_at TEXT,
                    event_count INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    refresh_interval_minutes INTEGER NOT NULL DEFAULT 60,
                    network_mode TEXT NOT NULL DEFAULT 'direct',
                    PRIMARY KEY(user_id, id)
                );
                CREATE TABLE IF NOT EXISTS reminder_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES puaa_users(id) ON DELETE CASCADE,
                    rule_key TEXT NOT NULL,
                    label TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    offsets TEXT NOT NULL,
                    channels TEXT NOT NULL,
                    UNIQUE(user_id, rule_key)
                );
                CREATE TABLE IF NOT EXISTS reminder_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES puaa_users(id) ON DELETE CASCADE,
                    event_id INTEGER NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,
                    rule_id INTEGER NOT NULL REFERENCES reminder_rules(id) ON DELETE CASCADE,
                    channel TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    UNIQUE(user_id, event_id, rule_id, channel, scheduled_at)
                );
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id INTEGER PRIMARY KEY REFERENCES puaa_users(id) ON DELETE CASCADE,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                    quiet_start TEXT,
                    quiet_end TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automation_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES puaa_users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    definition TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_automation_rules_user
                    ON automation_rules(user_id, enabled);
                CREATE TABLE IF NOT EXISTS notification_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES puaa_users(id) ON DELETE CASCADE,
                    type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    config_ciphertext TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unverified',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scheduled_triggers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES puaa_users(id) ON DELETE CASCADE,
                    rule_id INTEGER NOT NULL REFERENCES automation_rules(id) ON DELETE CASCADE,
                    event_id INTEGER NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,
                    action_index INTEGER NOT NULL,
                    occurrence INTEGER NOT NULL DEFAULT 1,
                    run_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    dedupe_key TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_triggers_due
                    ON scheduled_triggers(status, run_at);
                CREATE TABLE IF NOT EXISTS sync_snapshots (
                    user_id INTEGER NOT NULL REFERENCES puaa_users(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    events TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id,source)
                );
                CREATE TABLE IF NOT EXISTS sync_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES puaa_users(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    eligible_rules TEXT NOT NULL,
                    detected_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sync_changes_user ON sync_changes(user_id,id);
                CREATE TABLE IF NOT EXISTS change_triggers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES puaa_users(id) ON DELETE CASCADE,
                    rule_id INTEGER NOT NULL REFERENCES automation_rules(id) ON DELETE CASCADE,
                    change_id INTEGER NOT NULL REFERENCES sync_changes(id) ON DELETE CASCADE,
                    action_index INTEGER NOT NULL,
                    occurrence INTEGER NOT NULL DEFAULT 1,
                    run_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    dedupe_key TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_change_triggers_due ON change_triggers(status,run_at);
                CREATE TABLE IF NOT EXISTS notification_deliveries_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES puaa_users(id) ON DELETE CASCADE,
                    trigger_id INTEGER REFERENCES scheduled_triggers(id) ON DELETE SET NULL,
                    channel TEXT NOT NULL,
                    channel_id INTEGER REFERENCES notification_channels(id) ON DELETE SET NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    sent_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inbox_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES puaa_users(id) ON DELETE CASCADE,
                    delivery_id INTEGER REFERENCES notification_deliveries_v2(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    read_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER REFERENCES puaa_users(id) ON DELETE SET NULL,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT,
                    detail TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(user_sources)")}
            if "enabled" not in columns:
                self._connection.execute(
                    "ALTER TABLE user_sources ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
                )
            if "refresh_interval_minutes" not in columns:
                self._connection.execute(
                    "ALTER TABLE user_sources ADD COLUMN refresh_interval_minutes INTEGER NOT NULL DEFAULT 60"
                )
            if "network_mode" not in columns:
                self._connection.execute("ALTER TABLE user_sources ADD COLUMN network_mode TEXT NOT NULL DEFAULT 'direct'")
            # Older health alerts used the vague title “需要处理”, which could
            # be mistaken for a booking action.  Preserve history but make its
            # meaning explicit.
            self._connection.execute(
                "UPDATE inbox_notifications SET title=replace(title,'需要处理','同步异常') "
                "WHERE title LIKE '%需要处理'"
            )
            self._connection.execute(
                "UPDATE notification_deliveries_v2 SET title=replace(title,'需要处理','同步异常') "
                "WHERE title LIKE '%需要处理'"
            )
            trigger_columns = {
                row["name"] for row in self._connection.execute("PRAGMA table_info(scheduled_triggers)")
            }
            if "attempts" not in trigger_columns:
                self._connection.execute(
                    "ALTER TABLE scheduled_triggers ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
                )
            preference_columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(user_preferences)")}
            for name, definition in [("academic_filter_enabled", "INTEGER NOT NULL DEFAULT 1"),
                                     ("academic_start_date", "TEXT"), ("academic_end_date", "TEXT")]:
                if name not in preference_columns:
                    self._connection.execute(f"ALTER TABLE user_preferences ADD COLUMN {name} {definition}")
            event_columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(calendar_events)")}
            if "ignored" not in event_columns:
                self._connection.execute("ALTER TABLE calendar_events ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0")
            delivery_columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(notification_deliveries_v2)")}
            if "change_trigger_id" not in delivery_columns:
                self._connection.execute("ALTER TABLE notification_deliveries_v2 ADD COLUMN change_trigger_id INTEGER REFERENCES change_triggers(id) ON DELETE SET NULL")

    def create_user(self, email: str, display_name: str, password_hash: str) -> dict[str, Any]:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT INTO puaa_users(email, display_name, password_hash, created_at) VALUES(?,?,?,?)",
                (email.strip().lower(), display_name.strip(), password_hash, now_iso()),
            )
            user_id = int(cursor.lastrowid)
        self.seed_rules(user_id)
        self.ensure_preferences(user_id)
        return self.get_user(user_id) or {}

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM puaa_users WHERE id=? AND disabled_at IS NULL", (user_id,))

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM puaa_users WHERE email=? COLLATE NOCASE", (email.strip(),))

    def create_session(self, user_id: int, token_hash: str, csrf_hash: str, days: int) -> None:
        expires = (datetime.now(UTC) + timedelta(days=days)).isoformat()
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM puaa_sessions WHERE expires_at < ?", (now_iso(),))
            self._connection.execute(
                "INSERT INTO puaa_sessions(user_id, token_hash, csrf_hash, expires_at, created_at) VALUES(?,?,?,?,?)",
                (user_id, token_hash, csrf_hash, expires, now_iso()),
            )

    def session_user(self, token_hash: str) -> dict[str, Any] | None:
        return self._one(
            """SELECT u.*, s.csrf_hash, s.expires_at FROM puaa_sessions s
               JOIN puaa_users u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.expires_at>? AND u.disabled_at IS NULL""",
            (token_hash, now_iso()),
        )

    def delete_session(self, token_hash: str) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM puaa_sessions WHERE token_hash=?", (token_hash,))

    def upsert_school_connection(self, user_id: int, values: dict[str, Any]) -> dict[str, Any]:
        # Merge from the internal row so a status-only update never erases the
        # encrypted password that the public representation intentionally hides.
        current = self.school_secret(user_id)
        merged = {
            "school_id": values.get("school_id") or (current or {}).get("school_id"),
            "display_name": values.get("display_name", (current or {}).get("display_name")),
            "status": values.get("status", (current or {}).get("status", "login_required")),
            "remember_password": int(values.get("remember_password", (current or {}).get("remember_password", 0))),
            "password_ciphertext": values.get("password_ciphertext", (current or {}).get("password_ciphertext")),
            "last_login_at": values.get("last_login_at", (current or {}).get("last_login_at")),
            "last_sync_at": values.get("last_sync_at", (current or {}).get("last_sync_at")),
            "detail": values.get("detail", (current or {}).get("detail", "")),
        }
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO school_connections
                   (user_id, school_id, display_name, status, remember_password, password_ciphertext,
                    last_login_at, last_sync_at, detail) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET school_id=excluded.school_id,
                    display_name=excluded.display_name, status=excluded.status,
                    remember_password=excluded.remember_password,
                    password_ciphertext=excluded.password_ciphertext,
                    last_login_at=excluded.last_login_at, last_sync_at=excluded.last_sync_at,
                    detail=excluded.detail""",
                (user_id, *merged.values()),
            )
        return self.school_connection(user_id) or {}

    def school_connection(self, user_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM school_connections WHERE user_id=?", (user_id,))
        if row:
            row["remember_password"] = bool(row["remember_password"])
            row.pop("password_ciphertext", None)
        return row

    def school_secret(self, user_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM school_connections WHERE user_id=?", (user_id,))

    def disconnect_school(self, user_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM school_connections WHERE user_id=?", (user_id,))
            self._connection.execute("DELETE FROM user_sources WHERE user_id=?", (user_id,))

    def seed_rules(self, user_id: int) -> None:
        defaults = [
            ("course-start", "上课提醒", "course", 1, [-15], ["app"]),
            ("signin-missing", "未签到", "signin", 1, [-30], ["app"]),
            ("assignment-due", "作业截止", "assignment", 1, [-1440, -120], ["app"]),
            ("exam-start", "考试提醒", "exam", 1, [-10080, -1440], ["app"]),
        ]
        with self._lock, self._connection:
            self._connection.executemany(
                """INSERT OR IGNORE INTO reminder_rules
                   (user_id, rule_key, label, kind, enabled, offsets, channels)
                   VALUES(?,?,?,?,?,?,?)""",
                [(user_id, a, b, c, d, json.dumps(e), json.dumps(f)) for a, b, c, d, e, f in defaults],
            )

    def upsert_event(self, user_id: int, event: dict[str, Any]) -> None:
        values = (
            user_id, event["source"], event["external_id"], event["kind"], event["title"],
            event.get("starts_at"), event.get("ends_at"), event.get("due_at"),
            int(event.get("all_day", False)), event.get("location"), event.get("status", "pending"),
            json.dumps(event.get("metadata", {}), ensure_ascii=False), now_iso(),
        )
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO calendar_events
                   (user_id, source, external_id, kind, title, starts_at, ends_at, due_at,
                    all_day, location, status, metadata, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id, source, external_id) DO UPDATE SET
                    kind=excluded.kind, title=excluded.title, starts_at=excluded.starts_at,
                    ends_at=excluded.ends_at, due_at=excluded.due_at, all_day=excluded.all_day,
                    location=excluded.location, status=excluded.status,
                    metadata=excluded.metadata, updated_at=excluded.updated_at""",
                values,
            )

    def remove_missing_source_events(
        self, user_id: int, source: str, external_ids: set[str], kinds: tuple[str, ...] | None = None
    ) -> int:
        """Remove upstream rows no longer returned after a successful full sync."""
        with self._lock, self._connection:
            kind_clause = " AND kind IN (" + ",".join("?" for _ in kinds) + ")" if kinds else ""
            kind_values = tuple(kinds or ())
            if external_ids:
                placeholders = ",".join("?" for _ in external_ids)
                cursor = self._connection.execute(
                    f"DELETE FROM calendar_events WHERE user_id=? AND source=?{kind_clause} "
                    f"AND external_id NOT IN ({placeholders})",
                    (user_id, source, *kind_values, *sorted(external_ids)),
                )
            else:
                cursor = self._connection.execute(
                    f"DELETE FROM calendar_events WHERE user_id=? AND source=?{kind_clause}",
                    (user_id, source, *kind_values),
                )
        return cursor.rowcount

    def event_by_key(self, user_id: int, source: str, external_id: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM calendar_events WHERE user_id=? AND source=? AND external_id=?",
            (user_id, source, external_id),
        )
        return self._decode_event_dict(row) if row else None

    def event_by_id(self, user_id: int, event_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM calendar_events WHERE user_id=? AND id=?", (user_id, event_id))
        return self._decode_event_dict(row) if row else None

    def set_event_ignored(self, user_id: int, event_id: int, ignored: bool) -> dict[str, Any] | None:
        with self._lock, self._connection:
            cursor = self._connection.execute("UPDATE calendar_events SET ignored=? WHERE user_id=? AND id=?",
                                              (int(ignored), user_id, event_id))
            if cursor.rowcount and ignored:
                self._connection.execute(
                    "UPDATE scheduled_triggers SET status='cancelled',detail='已忽略',processed_at=? "
                    "WHERE user_id=? AND event_id=? AND status='queued'", (now_iso(), user_id, event_id))
                self._connection.execute(
                    "UPDATE change_triggers SET status='cancelled',detail='已忽略',processed_at=? "
                    "WHERE user_id=? AND status='queued' AND change_id IN "
                    "(SELECT c.id FROM sync_changes c JOIN calendar_events e ON "
                    "e.user_id=c.user_id AND e.source=c.source AND e.external_id=c.external_id WHERE e.id=?)",
                    (now_iso(), user_id, event_id))
        return self.event_by_id(user_id, event_id) if cursor.rowcount else None

    def create_custom_event(self, user_id: int, event: dict[str, Any]) -> dict[str, Any]:
        external_id = f"custom-{datetime.now(UTC).timestamp()}"
        self.upsert_event(user_id, {"source": "custom", "external_id": external_id, **event})
        return self._decode_event(self._one_row(
            "SELECT * FROM calendar_events WHERE user_id=? AND source='custom' AND external_id=?",
            (user_id, external_id),
        ))

    def list_events(self, user_id: int, start: str | None = None, end: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        clauses = ["user_id=?"]
        args: list[Any] = [user_id]
        if start:
            clauses.append("COALESCE(starts_at, due_at, ends_at) >= ?")
            args.append(start)
        if end:
            clauses.append("COALESCE(starts_at, due_at, ends_at) < ?")
            args.append(end)
        args.append(limit)
        rows = self._rows(
            f"""SELECT * FROM calendar_events WHERE {' AND '.join(clauses)}
                ORDER BY COALESCE(starts_at, due_at, ends_at, '9999') ASC LIMIT ?""",
            tuple(args),
        )
        return [self._decode_event_dict(row) for row in rows]

    def upsert_source(self, user_id: int, source: dict[str, Any]) -> None:
        default_intervals = {
            "iclass": 5, "grade": 15, "spoc": 30, "libbook": 30,
            "byxt": 60, "judge": 60, "bykc": 60, "cgyy": 60, "ygdk": 360,
        }
        refresh_interval = int(source.get(
            "refresh_interval_minutes", default_intervals.get(source["id"], 60)
        ))
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO user_sources(
                       user_id,id,label,status,detail,last_sync_at,event_count,refresh_interval_minutes
                   ) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,id) DO UPDATE SET
                    label=excluded.label,status=excluded.status,detail=excluded.detail,
                    last_sync_at=excluded.last_sync_at,event_count=excluded.event_count""",
                (user_id, source["id"], source["label"], source["status"], source.get("detail", ""),
                 source.get("last_sync_at"), source.get("event_count", 0), refresh_interval),
            )

    def list_sources(self, user_id: int) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM user_sources WHERE user_id=? ORDER BY label", (user_id,))
        for row in rows:
            row["enabled"] = bool(row["enabled"])
        return rows

    def get_source(self, user_id: int, source_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM user_sources WHERE user_id=? AND id=?", (user_id, source_id))
        if row:
            row["enabled"] = bool(row["enabled"])
        return row

    def set_source_enabled(self, user_id: int, source_id: str, enabled: bool) -> dict[str, Any] | None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE user_sources SET enabled=? WHERE user_id=? AND id=?",
                (int(enabled), user_id, source_id),
            )
            if cursor.rowcount and not enabled:
                self._connection.execute(
                    """UPDATE scheduled_triggers SET status='cancelled',detail='数据源已停用',processed_at=?
                       WHERE user_id=? AND status='queued' AND event_id IN
                         (SELECT id FROM calendar_events WHERE user_id=? AND source=?)""",
                    (now_iso(), user_id, user_id, source_id),
                )
                self._connection.execute(
                    "UPDATE change_triggers SET status='cancelled',detail='数据源已停用',processed_at=? "
                    "WHERE user_id=? AND status='queued' AND change_id IN "
                    "(SELECT id FROM sync_changes WHERE user_id=? AND source=?)",
                    (now_iso(), user_id, user_id, source_id))
        return self.get_source(user_id, source_id) if cursor.rowcount else None

    def update_source_settings(
        self, user_id: int, source_id: str, *, enabled: bool | None = None,
        refresh_interval_minutes: int | None = None,
        network_mode: str | None = None,
    ) -> dict[str, Any] | None:
        current = self.get_source(user_id, source_id)
        if not current:
            return None
        if network_mode is not None and (source_id != "judge" or network_mode not in {"direct", "webvpn"}):
            raise ValueError("此来源不支持该访问模式")
        if enabled is not None:
            self.set_source_enabled(user_id, source_id, enabled)
        if refresh_interval_minutes is not None:
            with self._lock, self._connection:
                self._connection.execute(
                    "UPDATE user_sources SET refresh_interval_minutes=? WHERE user_id=? AND id=?",
                    (refresh_interval_minutes, user_id, source_id),
                )
        if network_mode is not None and network_mode != current.get("network_mode", "direct"):
            with self._lock, self._connection:
                self._connection.execute(
                    "UPDATE user_sources SET network_mode=?,status='not_connected',detail='访问模式已更改，请同步',last_sync_at=NULL "
                    "WHERE user_id=? AND id=?", (network_mode, user_id, source_id))
        return self.get_source(user_id, source_id)

    def due_source_ids(self, user_id: int, at: datetime | None = None) -> list[str]:
        at = at or datetime.now(UTC)
        due: list[str] = []
        for source in self.list_sources(user_id):
            interval = int(source.get("refresh_interval_minutes") or 0)
            if not source["enabled"] or interval <= 0:
                continue
            last = source.get("last_sync_at")
            if not last:
                due.append(source["id"])
                continue
            try:
                refreshed = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if refreshed.tzinfo is None:
                    refreshed = refreshed.replace(tzinfo=UTC)
            except ValueError:
                due.append(source["id"])
                continue
            if refreshed + timedelta(minutes=interval) <= at:
                due.append(source["id"])
        return due

    def list_auto_sync_user_ids(self) -> list[int]:
        return [row["user_id"] for row in self._rows(
            """SELECT user_id FROM school_connections
               WHERE remember_password=1 AND password_ciphertext IS NOT NULL"""
        )]

    def list_rules(self, user_id: int) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM reminder_rules WHERE user_id=? ORDER BY id", (user_id,))
        for row in rows:
            row["enabled"] = bool(row["enabled"])
            row["offsets"] = json.loads(row["offsets"])
            row["channels"] = json.loads(row["channels"])
        return rows

    def update_rule(self, user_id: int, rule_key: str, values: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"enabled", "offsets", "channels"}
        fields, args = [], []
        for key, value in values.items():
            if key not in allowed or value is None:
                continue
            fields.append(f"{key}=?")
            args.append(int(value) if key == "enabled" else json.dumps(value))
        if fields:
            args.extend([user_id, rule_key])
            with self._lock, self._connection:
                self._connection.execute(
                    f"UPDATE reminder_rules SET {', '.join(fields)} WHERE user_id=? AND rule_key=?", args
                )
        return next((rule for rule in self.list_rules(user_id) if rule["rule_key"] == rule_key), None)

    def list_deliveries(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM reminder_deliveries WHERE user_id=? ORDER BY scheduled_at DESC LIMIT ?",
            (user_id, limit),
        )

    def ensure_preferences(self, user_id: int) -> dict[str, Any]:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO user_preferences(user_id,updated_at) VALUES(?,?)",
                (user_id, now_iso()),
            )
        return self.preferences(user_id)

    def preferences(self, user_id: int) -> dict[str, Any]:
        row = self._one("SELECT * FROM user_preferences WHERE user_id=?", (user_id,)) or {
            "user_id": user_id, "timezone": "Asia/Shanghai", "quiet_start": None, "quiet_end": None,
            "academic_filter_enabled": True, "academic_start_date": None, "academic_end_date": None,
        }
        row["academic_filter_enabled"] = bool(row["academic_filter_enabled"])
        return row

    def update_preferences(self, user_id: int, values: dict[str, Any]) -> dict[str, Any]:
        values = {**self.ensure_preferences(user_id), **values}
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE user_preferences SET timezone=?,quiet_start=?,quiet_end=?,
                   academic_filter_enabled=?,academic_start_date=?,academic_end_date=?,updated_at=?
                   WHERE user_id=?""",
                (values["timezone"], values.get("quiet_start"), values.get("quiet_end"),
                 int(values["academic_filter_enabled"]), values.get("academic_start_date"),
                 values.get("academic_end_date"), now_iso(), user_id),
            )
        return self.preferences(user_id)

    def create_automation_rule(self, user_id: int, definition: dict[str, Any]) -> dict[str, Any]:
        now = now_iso()
        payload = dict(definition)
        # Template ids are public string identifiers, never database ids.
        payload.pop("id", None)
        name = payload.pop("name")
        enabled = int(payload.pop("enabled", True))
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """INSERT INTO automation_rules(user_id,name,enabled,definition,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (user_id, name, enabled, json.dumps(payload, ensure_ascii=False), now, now),
            )
            rule_id = int(cursor.lastrowid)
        return self.get_automation_rule(user_id, rule_id) or {}

    def get_automation_rule(self, user_id: int, rule_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM automation_rules WHERE user_id=? AND id=?", (user_id, rule_id))
        return self._decode_rule(row) if row else None

    def list_automation_rules(self, user_id: int) -> list[dict[str, Any]]:
        return [self._decode_rule(row) for row in self._rows(
            "SELECT * FROM automation_rules WHERE user_id=? ORDER BY updated_at DESC", (user_id,)
        )]

    def update_automation_rule(self, user_id: int, rule_id: int, values: dict[str, Any]) -> dict[str, Any] | None:
        current = self.get_automation_rule(user_id, rule_id)
        if not current:
            return None
        merged = {**current, **{k: v for k, v in values.items() if v is not None}}
        name, enabled = merged["name"], int(merged["enabled"])
        definition = {k: merged[k] for k in (
            "scope", "trigger", "triggers", "conditions", "cancel_conditions", "repeat", "actions",
            "quiet_hours_policy", "profile", "profile_settings"
        ) if k in merged}
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE automation_rules SET name=?,enabled=?,definition=?,updated_at=? WHERE user_id=? AND id=?",
                (name, enabled, json.dumps(definition, ensure_ascii=False), now_iso(), user_id, rule_id),
            )
            self._connection.execute(
                "DELETE FROM scheduled_triggers WHERE user_id=? AND rule_id=? AND status='queued'",
                (user_id, rule_id),
            )
            self._connection.execute(
                "UPDATE change_triggers SET status='cancelled',detail='规则已修改',processed_at=? "
                "WHERE user_id=? AND rule_id=? AND status='queued'", (now_iso(), user_id, rule_id))
        return self.get_automation_rule(user_id, rule_id)

    def delete_automation_rule(self, user_id: int, rule_id: int) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM automation_rules WHERE user_id=? AND id=?", (user_id, rule_id)
            )
        return cursor.rowcount > 0

    def create_channel(self, user_id: int, channel_type: str, name: str, ciphertext: str, enabled: bool) -> dict[str, Any]:
        now = now_iso()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """INSERT INTO notification_channels
                   (user_id,type,name,enabled,config_ciphertext,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'unverified',?,?)""",
                (user_id, channel_type, name, int(enabled), ciphertext, now, now),
            )
            channel_id = int(cursor.lastrowid)
        return self.get_channel(user_id, channel_id, include_secret=False) or {}

    def get_channel(self, user_id: int, channel_id: int, include_secret: bool = False) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM notification_channels WHERE user_id=? AND id=?", (user_id, channel_id))
        if row and not include_secret:
            row.pop("config_ciphertext", None)
        if row:
            row["enabled"] = bool(row["enabled"])
        return row

    def list_channels(self, user_id: int) -> list[dict[str, Any]]:
        rows = self._rows(
            """SELECT id,user_id,type,name,enabled,status,last_error,created_at,updated_at
               FROM notification_channels WHERE user_id=? ORDER BY created_at""", (user_id,)
        )
        for row in rows:
            row["enabled"] = bool(row["enabled"])
        return rows

    def update_channel(self, user_id: int, channel_id: int, values: dict[str, Any]) -> dict[str, Any] | None:
        current = self.get_channel(user_id, channel_id, include_secret=True)
        if not current:
            return None
        fields, args = [], []
        for key in ("name", "enabled", "config_ciphertext"):
            if key in values and values[key] is not None:
                fields.append(f"{key}=?")
                args.append(int(values[key]) if key == "enabled" else values[key])
        if fields:
            fields.extend(["status='unverified'", "last_error=''", "updated_at=?"])
            args.extend([now_iso(), user_id, channel_id])
            with self._lock, self._connection:
                self._connection.execute(
                    f"UPDATE notification_channels SET {','.join(fields)} WHERE user_id=? AND id=?", args
                )
        return self.get_channel(user_id, channel_id)

    def set_channel_status(self, user_id: int, channel_id: int, status: str, error: str = "") -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE notification_channels SET status=?,last_error=?,updated_at=? WHERE user_id=? AND id=?",
                (status, error[:300], now_iso(), user_id, channel_id),
            )

    def delete_channel(self, user_id: int, channel_id: int) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM notification_channels WHERE user_id=? AND id=?", (user_id, channel_id)
            )
        return cursor.rowcount > 0

    def cancel_rule_triggers(self, user_id: int, rule_id: int, detail: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE scheduled_triggers SET status='cancelled',detail=?,processed_at=? "
                "WHERE user_id=? AND rule_id=? AND status='queued'",
                (detail, now_iso(), user_id, rule_id),
            )
            self._connection.execute(
                "UPDATE change_triggers SET status='cancelled',detail=?,processed_at=? "
                "WHERE user_id=? AND rule_id=? AND status='queued'", (detail, now_iso(), user_id, rule_id))

    def get_sync_snapshot(self, user_id: int, source: str) -> dict[str, Any] | None:
        row = self._one("SELECT events FROM sync_snapshots WHERE user_id=? AND source=?", (user_id, source))
        return json.loads(row["events"]) if row else None

    def commit_sync_snapshot(self, user_id: int, source: str, events: dict, changes: list[dict], detected_at: str) -> None:
        from app.features import CHANGE_PROFILES, rule_available
        eligible = [rule["id"] for rule in self.list_automation_rules(user_id)
                    if rule["enabled"] and rule.get("profile") in CHANGE_PROFILES and rule_available(rule)
                    and source in rule["scope"]["sources"]]
        with self._lock, self._connection:
            for change in changes:
                recipients = [] if change["event"].get("ignored") else eligible
                self._connection.execute(
                    "INSERT INTO sync_changes(user_id,source,external_id,payload,eligible_rules,detected_at) VALUES(?,?,?,?,?,?)",
                    (user_id, source, change["event"]["external_id"], json.dumps(change, ensure_ascii=False),
                     json.dumps(recipients), detected_at))
            self._connection.execute(
                "INSERT INTO sync_snapshots(user_id,source,events,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id,source) DO UPDATE SET events=excluded.events,updated_at=excluded.updated_at",
                (user_id, source, json.dumps(events, ensure_ascii=False), detected_at))

    def list_sync_changes(self, user_id: int) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM sync_changes WHERE user_id=? ORDER BY id", (user_id,))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
            row["eligible_rules"] = json.loads(row["eligible_rules"])
        return rows

    def queue_change_trigger(self, user_id: int, rule_id: int, change_id: int, action_index: int, run_at: str) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO change_triggers(user_id,rule_id,change_id,action_index,run_at,created_at,dedupe_key) "
                "VALUES(?,?,?,?,?,?,?)", (user_id, rule_id, change_id, action_index, run_at, now_iso(),
                                         f"{user_id}:{rule_id}:{change_id}:{action_index}"))
        return cursor.rowcount

    def replace_triggers_for_rule(self, user_id: int, rule_id: int, triggers: list[dict[str, Any]]) -> int:
        inserted = 0
        with self._lock, self._connection:
            desired = [trigger["dedupe_key"] for trigger in triggers]
            if desired:
                placeholders = ",".join("?" for _ in desired)
                self._connection.execute(
                    f"""DELETE FROM scheduled_triggers
                        WHERE user_id=? AND rule_id=? AND status='queued'
                          AND dedupe_key NOT IN ({placeholders})""",
                    [user_id, rule_id, *desired],
                )
            else:
                self._connection.execute(
                    "DELETE FROM scheduled_triggers WHERE user_id=? AND rule_id=? AND status='queued'",
                    (user_id, rule_id),
                )
            for trigger in triggers:
                # Restore only future ignored plans, never resend delivered work.
                self._connection.execute(
                    "UPDATE scheduled_triggers SET status='queued',detail='',processed_at=NULL,attempts=0 "
                    "WHERE user_id=? AND rule_id=? AND dedupe_key=? AND status='cancelled' AND detail='已忽略' "
                    "AND run_at>=?", (user_id, rule_id, trigger["dedupe_key"], now_iso()))
                cursor = self._connection.execute(
                    """INSERT OR IGNORE INTO scheduled_triggers
                       (user_id,rule_id,event_id,action_index,occurrence,run_at,status,created_at,dedupe_key)
                       VALUES(?,?,?,?,?,?,'queued',?,?)""",
                    (user_id, rule_id, trigger["event_id"], trigger["action_index"], trigger["occurrence"],
                     trigger["run_at"], now_iso(), trigger["dedupe_key"]),
                )
                inserted += cursor.rowcount
        return inserted

    def list_triggers(self, user_id: int, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._rows(
            """SELECT t.*,r.name AS rule_name,e.title AS event_title
               FROM scheduled_triggers t JOIN automation_rules r ON r.id=t.rule_id
               JOIN calendar_events e ON e.id=t.event_id
               WHERE t.user_id=? ORDER BY t.run_at LIMIT ?""", (user_id, limit)
        )
        changed = self._rows(
            "SELECT -t.id AS id,t.user_id,t.rule_id,t.change_id,t.action_index,t.occurrence,t.run_at,t.status,t.detail,"
            "t.attempts,r.name AS rule_name,json_extract(c.payload,'$.event.title') AS event_title "
            "FROM change_triggers t JOIN automation_rules r ON r.id=t.rule_id JOIN sync_changes c ON c.id=t.change_id "
            "WHERE t.user_id=? ORDER BY t.run_at LIMIT ?", (user_id, limit))
        return sorted(rows + changed, key=lambda row: row["run_at"])[:limit]

    def due_triggers(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM scheduled_triggers WHERE status='queued' AND run_at<=? ORDER BY run_at LIMIT ?",
            (now_iso(), limit),
        )
        changed = self._rows("SELECT * FROM change_triggers WHERE status='queued' AND run_at<=? ORDER BY run_at LIMIT ?",
                             (now_iso(), limit))
        # Negative public ids distinguish the durable change queue without
        # rebuilding the existing event-trigger table or its foreign keys.
        for row in changed:
            row["id"] = -row["id"]
        return sorted(rows + changed, key=lambda row: row["run_at"])[:limit]

    def trigger_context(self, trigger_id: int) -> dict[str, Any] | None:
        if trigger_id < 0:
            row = self._one(
                "SELECT t.*,r.name AS rule_name,r.definition,r.enabled AS rule_enabled,c.source,c.external_id,c.payload "
                "FROM change_triggers t JOIN automation_rules r ON r.id=t.rule_id "
                "JOIN sync_changes c ON c.id=t.change_id WHERE t.id=?", (-trigger_id,))
            if not row:
                return None
            payload = json.loads(row.pop("payload"))
            event = payload["event"]
            current = self.event_by_key(row["user_id"], row["source"], row["external_id"])
            row.update({key: event.get(key) for key in ("title", "kind", "starts_at", "ends_at", "due_at", "location")})
            row.update(id=trigger_id, event_status=(current or event).get("status", "unknown"),
                       ignored=(current or event).get("ignored", False),
                       metadata=(current or event).get("metadata", {}), change=payload,
                       definition=json.loads(row["definition"]))
            return row
        row = self._one(
            """SELECT t.*,r.name AS rule_name,r.definition,r.enabled AS rule_enabled,
                      e.title,e.kind,e.source,e.starts_at,e.ends_at,e.due_at,e.status AS event_status,
                      e.location,e.metadata,e.ignored
               FROM scheduled_triggers t JOIN automation_rules r ON r.id=t.rule_id
               JOIN calendar_events e ON e.id=t.event_id WHERE t.id=?""", (trigger_id,)
        )
        if row:
            row["definition"] = json.loads(row["definition"])
            row["metadata"] = json.loads(row["metadata"])
        return row

    def finish_trigger(self, trigger_id: int, status: str, detail: str = "") -> None:
        table = "change_triggers" if trigger_id < 0 else "scheduled_triggers"
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE {table} SET status=?,detail=?,processed_at=? WHERE id=?",
                (status, detail[:300], now_iso(), abs(trigger_id)),
            )

    def reschedule_trigger(
        self, trigger_id: int, run_at: str, detail: str = "", increment_attempts: bool = False
    ) -> None:
        table = "change_triggers" if trigger_id < 0 else "scheduled_triggers"
        with self._lock, self._connection:
            self._connection.execute(
                f"""UPDATE {table} SET run_at=?,detail=?,attempts=attempts+?
                   WHERE id=? AND status='queued'""",
                (run_at, detail[:300], int(increment_attempts), abs(trigger_id)),
            )

    def create_delivery(self, values: dict[str, Any]) -> int:
        trigger_id = values.get("trigger_id")
        change_id = -trigger_id if trigger_id is not None and trigger_id < 0 else None
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """INSERT INTO notification_deliveries_v2
                   (user_id,trigger_id,channel,channel_id,title,body,status,error,sent_at,created_at,change_trigger_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (values["user_id"], None if change_id else trigger_id, values["channel"], values.get("channel_id"),
                 values["title"], values["body"], values["status"], values.get("error", ""),
                 values.get("sent_at"), now_iso(), change_id),
            )
        return int(cursor.lastrowid)

    def create_inbox(self, user_id: int, delivery_id: int, title: str, body: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO inbox_notifications(user_id,delivery_id,title,body,created_at) VALUES(?,?,?,?,?)",
                (user_id, delivery_id, title, body, now_iso()),
            )

    def list_delivery_v2(self, user_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM notification_deliveries_v2 WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )

    def list_inbox(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM inbox_notifications WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )

    def audit(self, user_id: int | None, action: str, target_type: str, target_id: str | None = None, detail: dict[str, Any] | None = None) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO audit_events(user_id,action,target_type,target_id,detail,created_at) VALUES(?,?,?,?,?,?)",
                (user_id, action, target_type, target_id, json.dumps(detail or {}, ensure_ascii=False), now_iso()),
            )

    def queue_delivery(self, user_id: int, event_id: int, rule_id: int, channel: str, scheduled_at: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT OR IGNORE INTO reminder_deliveries
                   (user_id,event_id,rule_id,channel,scheduled_at,status) VALUES(?,?,?,?,?,'queued')""",
                (user_id, event_id, rule_id, channel, scheduled_at),
            )

    def dashboard(self, user_id: int) -> dict[str, Any]:
        return {
            "events": self.list_events(user_id),
            "sources": self.list_sources(user_id),
            "rules": self.list_automation_rules(user_id),
            "channels": self.list_channels(user_id),
            "deliveries": self.list_delivery_v2(user_id, 20),
            "inbox": self.list_inbox(user_id, 20),
            "preferences": self.ensure_preferences(user_id),
            "school": self.school_connection(user_id),
        }

    def _one(self, sql: str, args: tuple[Any, ...]) -> dict[str, Any] | None:
        row = self._one_row(sql, args)
        return dict(row) if row else None

    def _one_row(self, sql: str, args: tuple[Any, ...]) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(sql, args).fetchone()

    def _rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._connection.execute(sql, args).fetchall()]

    @staticmethod
    def _decode_event(row: sqlite3.Row | None) -> dict[str, Any]:
        return Database._decode_event_dict(dict(row)) if row else {}

    @staticmethod
    def _decode_event_dict(value: dict[str, Any]) -> dict[str, Any]:
        value["metadata"] = json.loads(value["metadata"])
        value["all_day"] = bool(value["all_day"])
        value["ignored"] = bool(value.get("ignored"))
        return value

    @staticmethod
    def _decode_rule(value: dict[str, Any]) -> dict[str, Any]:
        definition = json.loads(value.pop("definition"))
        definition.pop("id", None)
        value["enabled"] = bool(value["enabled"])
        value.update(definition)
        return value
