"""SyncBridge 同步 API。

实现依据：《SyncBridge 方案 A》6.2 API、6.3 增量同步协议、9 认证与安全。

关键约定：
- 所有登录后接口从 access token 解析 account_id，绝不信任客户端传入的账号字段；
- 每个账号维护单调递增游标（sync_changes.seq），客户端只在处理成功后保存 nextCursor；
- 更新携带 version 时做乐观锁校验，冲突返回 409，不静默覆盖；
- 删除为软删除，写入 deleted_items 供回收站与恢复。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from . import schema as schema_module

APP_VERSION = "1.0.0"
API_VERSION = "v1"
ACCESS_TOKEN_TTL_MINUTES = 120
REFRESH_TOKEN_TTL_DAYS = 30
SOFT_DELETE_RETENTION_DAYS = 30

DB_PATH = Path(
    os.getenv("SyncBridge_DB_PATH", Path(__file__).resolve().parent.parent / "syncbridge.db")
)

app = FastAPI(title="SyncBridge Sync API", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    # Vite 开发页、Tauri 1 与 Tauri 2 的 WebView 来源。
    # Tauri 2 默认使用 http://tauri.localhost；缺少该来源时，
    # 登录这种带 JSON 的 POST 会在 CORS 预检阶段直接 Failed to fetch。
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
        "null",
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-SyncBridge-Version"],
)


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db() -> list[str]:
    with db() as connection:
        return schema_module.migrate(connection)


@app.on_event("startup")
def startup() -> None:
    init_db()
    from . import reminder as reminder_module

    reminder_module.start_scheduler()


@app.on_event("shutdown")
def shutdown() -> None:
    from . import reminder as reminder_module

    reminder_module.stop_scheduler()


# --------------------------------------------------------------------------
# 请求模型
# --------------------------------------------------------------------------
class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=6)


class RefreshRequest(BaseModel):
    refresh_token: str


class DeviceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    platform: str = Field(min_length=1, max_length=40)


class EventInput(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    location: str = ""
    start_at: str
    end_at: str
    timezone: str = "Asia/Shanghai"
    all_day: bool = False
    recurrence_rule: str | None = None
    reminder_minutes: list[int] = Field(default_factory=list)
    is_pinned: bool = False
    source: str = "syncbridge"
    source_id: str | None = None
    version: int | None = None

    @field_validator("reminder_minutes")
    @classmethod
    def valid_reminders(cls, value: list[int]) -> list[int]:
        cleaned: list[int] = []
        for item in value:
            if not isinstance(item, int) or item < 0 or item > 40_320:
                raise ValueError("提醒分钟数必须在 0 到 40320 之间")
            if item not in cleaned:
                cleaned.append(item)
        return sorted(cleaned)


class NoteInput(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    is_pinned: bool = False
    is_archived: bool = False
    source: str = "syncbridge"
    source_id: str | None = None
    version: int | None = None

    @field_validator("source")
    @classmethod
    def valid_source(cls, value: str) -> str:
        return value[:60]

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = str(item).strip()[:40]
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned[:20]


# --------------------------------------------------------------------------
# 认证
# --------------------------------------------------------------------------
def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 210_000)
    return f"pbkdf2$210000${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = stored.split("$", 3)
        if algorithm != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
        return secrets.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _issue_tokens(connection: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    access_token = secrets.token_urlsafe(32)
    refresh_token = secrets.token_urlsafe(48)
    access_expires = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_TTL_MINUTES)
    refresh_expires = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_TTL_DAYS)
    stamp = now_iso()
    connection.execute(
        "INSERT INTO tokens(token, user_id, expires_at, created_at) VALUES (?,?,?,?)",
        (access_token, user_id, access_expires.isoformat(), stamp),
    )
    connection.execute(
        "INSERT INTO refresh_tokens(token, user_id, expires_at, revoked, created_at) VALUES (?,?,?,0,?)",
        (refresh_token, user_id, refresh_expires.isoformat(), stamp),
    )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_TTL_MINUTES * 60,
        "user_id": user_id,
    }


def current_user(authorization: str | None = Header(default=None)) -> sqlite3.Row:
    """从 access token 解析账号；不接受客户端传入的账号字段。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="登录已失效")
    token = authorization.removeprefix("Bearer ")
    with db() as connection:
        row = connection.execute(
            """
            SELECT users.*, tokens.expires_at AS token_expires_at
            FROM tokens JOIN users ON users.id = tokens.user_id
            WHERE tokens.token = ?
            """,
            (token,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="登录已失效")
    if row["token_expires_at"] and row["token_expires_at"] < now_iso():
        raise HTTPException(status_code=401, detail="登录已过期，请刷新令牌")
    return row


# --------------------------------------------------------------------------
# 变更日志
# --------------------------------------------------------------------------
def record_change(
    connection: sqlite3.Connection,
    account_id: str,
    entity_type: str,
    entity_id: str,
    operation: str,
) -> int:
    """追加一条变更日志，返回新的游标值。"""
    next_seq = connection.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM sync_changes WHERE account_id=?",
        (account_id,),
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO sync_changes(account_id, seq, entity_type, entity_id, operation, changed_at) "
        "VALUES (?,?,?,?,?,?)",
        (account_id, next_seq, entity_type, entity_id, operation, now_iso()),
    )
    return next_seq


def current_cursor(connection: sqlite3.Connection, account_id: str) -> int:
    return connection.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM sync_changes WHERE account_id=?", (account_id,)
    ).fetchone()[0]


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------
def public_event(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "location": row["location"],
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "timezone": row["timezone"],
        "all_day": bool(row["all_day"]),
        "recurrence_rule": row["recurrence_rule"],
        "reminder_minutes": json.loads(row["reminder_minutes"] or "[]"),
        "is_pinned": bool(row["is_pinned"]),
        "source": row["source"] if "source" in keys else "syncbridge",
        "source_id": row["source_id"] if "source_id" in keys else None,
        "version": row["version"],
        "updated_at": row["updated_at"],
        "deleted_at": row["deleted_at"],
    }


def public_note(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    return {
        "id": row["id"],
        "title": row["title"],
        "content": row["content"],
        "tags": json.loads(row["tags"] or "[]"),
        "is_pinned": bool(row["is_pinned"]),
        "is_archived": bool(row["is_archived"]),
        "source": row["source"] if "source" in keys else "syncbridge",
        "source_id": row["source_id"] if "source_id" in keys else None,
        "version": row["version"],
        "updated_at": row["updated_at"],
        "deleted_at": row["deleted_at"],
    }


# --------------------------------------------------------------------------
# 健康检查 / 认证 / 设备
# --------------------------------------------------------------------------
@app.get("/api/v1/health")
def health(response: Response) -> dict[str, str]:
    response.headers["X-SyncBridge-Version"] = APP_VERSION
    return {
        "service": "syncbridge",
        "version": APP_VERSION,
        "apiVersion": API_VERSION,
        "status": "ok",
    }


@app.post("/api/v1/auth/login")
def login(payload: LoginRequest) -> dict[str, Any]:
    with db() as connection:
        user = connection.execute(
            "SELECT * FROM users WHERE email=?", (payload.email.lower(),)
        ).fetchone()
        if user and not verify_password(payload.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="邮箱或密码错误")
        if not user:
            user_id = secrets.token_urlsafe(16)
            connection.execute(
                "INSERT INTO users (id,email,password_hash,role,created_at) VALUES (?,?,?,?,?)",
                (user_id, payload.email.lower(), password_hash(payload.password), "admin", now_iso()),
            )
        else:
            user_id = user["id"]
        return _issue_tokens(connection, user_id)


@app.post("/api/v1/auth/refresh")
def refresh(payload: RefreshRequest) -> dict[str, Any]:
    with db() as connection:
        row = connection.execute(
            "SELECT user_id, expires_at, revoked FROM refresh_tokens WHERE token=?",
            (payload.refresh_token,),
        ).fetchone()
        if not row or row["revoked"]:
            raise HTTPException(status_code=401, detail="刷新令牌无效，请重新登录")
        if row["expires_at"] < now_iso():
            raise HTTPException(status_code=401, detail="刷新令牌已过期，请重新登录")
        # 轮换：旧刷新令牌立即吊销，确保一个令牌只能用一次。
        # 若不吊销，泄露的刷新令牌可在 30 天内无限重放，轮换机制形同虚设。
        connection.execute(
            "UPDATE refresh_tokens SET revoked=1 WHERE token=?", (payload.refresh_token,)
        )
        connection.execute("DELETE FROM tokens WHERE user_id=?", (row["user_id"],))
        return _issue_tokens(connection, row["user_id"])


@app.post("/api/v1/auth/logout")
def logout(
    payload: RefreshRequest, user: sqlite3.Row = Depends(current_user)
) -> dict[str, bool]:
    """登出：吊销该刷新令牌并清空该账号的全部访问令牌。

    仅凭 access token 鉴权即可完成，不要求刷新令牌仍然有效——
    这样即便刷新令牌已被轮换或被判定为泄露，登出依然可用。
    """
    with db() as connection:
        connection.execute(
            "UPDATE refresh_tokens SET revoked=1 WHERE token=? AND user_id=?",
            (payload.refresh_token, user["id"]),
        )
        # 兜底：该账号下所有未吊销的刷新令牌一并作废，避免残留可用凭据
        connection.execute(
            "UPDATE refresh_tokens SET revoked=1 WHERE user_id=? AND revoked=0",
            (user["id"],),
        )
        connection.execute("DELETE FROM tokens WHERE user_id=?", (user["id"],))
    return {"revoked": True}


@app.post("/api/v1/devices/register")
def register_device(
    payload: DeviceRequest, user: sqlite3.Row = Depends(current_user)
) -> dict[str, str]:
    device_id = secrets.token_urlsafe(12)
    stamp = now_iso()
    with db() as connection:
        connection.execute(
            "INSERT INTO devices(id,user_id,name,platform,last_seen_at,created_at) VALUES (?,?,?,?,?,?)",
            (device_id, user["id"], payload.name, payload.platform, stamp, stamp),
        )
    return {"device_id": device_id}


@app.get("/api/v1/admin/overview")
def admin_overview(user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    with db() as connection:
        counts = {
            "users": connection.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "devices": connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0],
            "events": connection.execute(
                "SELECT COUNT(*) FROM events WHERE deleted_at IS NULL"
            ).fetchone()[0],
            "notes": connection.execute(
                "SELECT COUNT(*) FROM notes WHERE deleted_at IS NULL"
            ).fetchone()[0],
            "pending_changes": connection.execute(
                "SELECT COUNT(*) FROM sync_changes WHERE account_id=?", (user["id"],)
            ).fetchone()[0],
        }
    return {
        "service": "syncbridge",
        "database": "sqlite",
        "status": "ok",
        "counts": counts,
        "cursor": counts["pending_changes"],
        "server_time": now_iso(),
    }


@app.get("/api/v1/admin/database/health")
def database_health(user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    with db() as connection:
        connection.execute("SELECT 1").fetchone()
        size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return {
        "status": "ok",
        "path": str(DB_PATH),
        "size_bytes": size,
        "schema_version": schema_module.SCHEMA_VERSION,
    }


# --------------------------------------------------------------------------
# 冲突检测
# --------------------------------------------------------------------------
def _assert_version(
    connection: sqlite3.Connection, table: str, entity_id: str, account_id: str,
    expected: int | None,
) -> sqlite3.Row:
    row = connection.execute(
        f"SELECT * FROM {table} WHERE id=? AND account_id=?", (entity_id, account_id)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    if expected is not None and int(expected) != int(row["version"]):
        raise HTTPException(
            status_code=409,
            detail=f"版本冲突：服务端当前 version={row['version']}，客户端提交 version={expected}",
        )
    return row


# --------------------------------------------------------------------------
# 事件
# --------------------------------------------------------------------------
@app.get("/api/v1/events")
def list_events(
    user: sqlite3.Row = Depends(current_user),
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    where = "" if include_deleted else " AND deleted_at IS NULL"
    with db() as connection:
        rows = connection.execute(
            f"SELECT * FROM events WHERE account_id=?{where} ORDER BY start_at", (user["id"],)
        ).fetchall()
    return [public_event(row) for row in rows]


@app.post("/api/v1/events", status_code=201)
def create_event(
    payload: EventInput, user: sqlite3.Row = Depends(current_user)
) -> dict[str, Any]:
    if payload.end_at < payload.start_at:
        raise HTTPException(status_code=422, detail="结束时间不能早于开始时间")
    with db() as connection:
        # 文档 9.4 幂等：优先用 source + source_id 判重。
        # 安卓端上传约定 source=android_calendar、source_id=calendarId:localEventId，
        # 重复同步同一本地事件时不得再创建一条新记录，直接返回已存在的一条。
        if payload.source_id:
            existing = connection.execute(
                "SELECT * FROM events WHERE account_id=? AND source=? AND source_id=?",
                (user["id"], payload.source, payload.source_id),
            ).fetchone()
            if existing:
                return public_event(existing)
        event_id, stamp = secrets.token_urlsafe(16), now_iso()
        connection.execute(
            """INSERT INTO events
               (id,account_id,title,description,location,start_at,end_at,all_day,timezone,
                recurrence_rule,reminder_minutes,is_pinned,source,source_id,version,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
            (
                event_id, user["id"], payload.title, payload.description, payload.location,
                payload.start_at, payload.end_at, int(payload.all_day), payload.timezone,
                payload.recurrence_rule, json.dumps(payload.reminder_minutes),
                int(payload.is_pinned), payload.source, payload.source_id, stamp,
            ),
        )
        record_change(connection, user["id"], "event", event_id, "upsert")
        row = connection.execute(
            "SELECT * FROM events WHERE id=? AND account_id=?", (event_id, user["id"])
        ).fetchone()
    return public_event(row)


@app.patch("/api/v1/events/{event_id}")
def update_event(
    event_id: str, payload: EventInput, user: sqlite3.Row = Depends(current_user)
) -> dict[str, Any]:
    if payload.end_at < payload.start_at:
        raise HTTPException(status_code=422, detail="结束时间不能早于开始时间")
    with db() as connection:
        _assert_version(connection, "events", event_id, user["id"], payload.version)
        stamp = now_iso()
        connection.execute(
            """UPDATE events SET title=?,description=?,location=?,start_at=?,end_at=?,
               all_day=?,timezone=?,recurrence_rule=?,reminder_minutes=?,is_pinned=?,
               source=?,source_id=?,version=version+1,updated_at=?,deleted_at=NULL
               WHERE id=? AND account_id=?""",
            (
                payload.title, payload.description, payload.location, payload.start_at,
                payload.end_at, int(payload.all_day), payload.timezone,
                payload.recurrence_rule, json.dumps(payload.reminder_minutes),
                int(payload.is_pinned), payload.source, payload.source_id, stamp,
                event_id, user["id"],
            ),
        )
        # 恢复出回收站的情况也要清掉删除记录
        connection.execute(
            "DELETE FROM deleted_items WHERE account_id=? AND entity_type='event' AND entity_id=?",
            (user["id"], event_id),
        )
        record_change(connection, user["id"], "event", event_id, "upsert")
        row = connection.execute(
            "SELECT * FROM events WHERE id=? AND account_id=?", (event_id, user["id"])
        ).fetchone()
    return public_event(row)


@app.delete("/api/v1/events/{event_id}")
def delete_event(
    event_id: str, user: sqlite3.Row = Depends(current_user), purge: bool = False
) -> dict[str, bool]:
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM events WHERE id=? AND account_id=?", (event_id, user["id"])
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="事件不存在")
        stamp = now_iso()
        if purge:
            connection.execute("DELETE FROM events WHERE id=? AND account_id=?", (event_id, user["id"]))
        else:
            purge_after = (
                datetime.now(timezone.utc) + timedelta(days=SOFT_DELETE_RETENTION_DAYS)
            ).isoformat()
            connection.execute(
                "UPDATE events SET deleted_at=?,version=version+1,updated_at=? WHERE id=? AND account_id=?",
                (stamp, stamp, event_id, user["id"]),
            )
            connection.execute(
                """INSERT INTO deleted_items(account_id,entity_type,entity_id,deleted_at,purge_after)
                   VALUES (?,'event',?,?,?)
                   ON CONFLICT(account_id,entity_type,entity_id)
                   DO UPDATE SET deleted_at=excluded.deleted_at, purge_after=excluded.purge_after""",
                (user["id"], event_id, stamp, purge_after),
            )
        record_change(connection, user["id"], "event", event_id, "delete")
    return {"deleted": True}


@app.post("/api/v1/events/{event_id}/restore")
def restore_event(
    event_id: str, user: sqlite3.Row = Depends(current_user)
) -> dict[str, Any]:
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM events WHERE id=? AND account_id=?", (event_id, user["id"])
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="事件不存在")
        connection.execute(
            "UPDATE events SET deleted_at=NULL,version=version+1,updated_at=? WHERE id=? AND account_id=?",
            (now_iso(), event_id, user["id"]),
        )
        connection.execute(
            "DELETE FROM deleted_items WHERE account_id=? AND entity_type='event' AND entity_id=?",
            (user["id"], event_id),
        )
        record_change(connection, user["id"], "event", event_id, "upsert")
        row = connection.execute(
            "SELECT * FROM events WHERE id=? AND account_id=?", (event_id, user["id"])
        ).fetchone()
    return public_event(row)


# --------------------------------------------------------------------------
# 便签
# --------------------------------------------------------------------------
@app.get("/api/v1/notes")
def list_notes(
    user: sqlite3.Row = Depends(current_user), include_deleted: bool = False
) -> list[dict[str, Any]]:
    where = "" if include_deleted else " AND deleted_at IS NULL"
    with db() as connection:
        rows = connection.execute(
            f"SELECT * FROM notes WHERE account_id=?{where} ORDER BY is_pinned DESC, updated_at DESC",
            (user["id"],),
        ).fetchall()
    return [public_note(row) for row in rows]


@app.post("/api/v1/notes", status_code=201)
def create_note(payload: NoteInput, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        if payload.source_id:
            existing = connection.execute(
                "SELECT * FROM notes WHERE account_id=? AND source=? AND source_id=?",
                (user["id"], payload.source, payload.source_id),
            ).fetchone()
            if existing:
                # 同来源同 ID 不重复导入，直接返回已有记录
                return public_note(existing)
        note_id, stamp = secrets.token_urlsafe(16), now_iso()
        connection.execute(
            """INSERT INTO notes
               (id,account_id,title,content,tags,is_pinned,is_archived,source,source_id,version,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,1,?)""",
            (
                note_id, user["id"], payload.title, payload.content,
                json.dumps(payload.tags, ensure_ascii=False), int(payload.is_pinned),
                int(payload.is_archived), payload.source, payload.source_id, stamp,
            ),
        )
        record_change(connection, user["id"], "note", note_id, "upsert")
        row = connection.execute(
            "SELECT * FROM notes WHERE id=? AND account_id=?", (note_id, user["id"])
        ).fetchone()
    return public_note(row)


@app.patch("/api/v1/notes/{note_id}")
def update_note(
    note_id: str, payload: NoteInput, user: sqlite3.Row = Depends(current_user)
) -> dict[str, Any]:
    with db() as connection:
        _assert_version(connection, "notes", note_id, user["id"], payload.version)
        connection.execute(
            """UPDATE notes SET title=?,content=?,tags=?,is_pinned=?,is_archived=?,
               source=?,source_id=?,version=version+1,updated_at=?,deleted_at=NULL
               WHERE id=? AND account_id=?""",
            (
                payload.title, payload.content, json.dumps(payload.tags, ensure_ascii=False),
                int(payload.is_pinned), int(payload.is_archived), payload.source,
                payload.source_id, now_iso(), note_id, user["id"],
            ),
        )
        connection.execute(
            "DELETE FROM deleted_items WHERE account_id=? AND entity_type='note' AND entity_id=?",
            (user["id"], note_id),
        )
        record_change(connection, user["id"], "note", note_id, "upsert")
        row = connection.execute(
            "SELECT * FROM notes WHERE id=? AND account_id=?", (note_id, user["id"])
        ).fetchone()
    return public_note(row)


@app.delete("/api/v1/notes/{note_id}")
def delete_note(
    note_id: str, user: sqlite3.Row = Depends(current_user), purge: bool = False
) -> dict[str, bool]:
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM notes WHERE id=? AND account_id=?", (note_id, user["id"])
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="便签不存在")
        stamp = now_iso()
        if purge:
            connection.execute("DELETE FROM notes WHERE id=? AND account_id=?", (note_id, user["id"]))
        else:
            purge_after = (
                datetime.now(timezone.utc) + timedelta(days=SOFT_DELETE_RETENTION_DAYS)
            ).isoformat()
            connection.execute(
                "UPDATE notes SET deleted_at=?,version=version+1,updated_at=? WHERE id=? AND account_id=?",
                (stamp, stamp, note_id, user["id"]),
            )
            connection.execute(
                """INSERT INTO deleted_items(account_id,entity_type,entity_id,deleted_at,purge_after)
                   VALUES (?,'note',?,?,?)
                   ON CONFLICT(account_id,entity_type,entity_id)
                   DO UPDATE SET deleted_at=excluded.deleted_at, purge_after=excluded.purge_after""",
                (user["id"], note_id, stamp, purge_after),
            )
        record_change(connection, user["id"], "note", note_id, "delete")
    return {"deleted": True}


@app.post("/api/v1/notes/{note_id}/restore")
def restore_note(note_id: str, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM notes WHERE id=? AND account_id=?", (note_id, user["id"])
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="便签不存在")
        connection.execute(
            "UPDATE notes SET deleted_at=NULL,version=version+1,updated_at=? WHERE id=? AND account_id=?",
            (now_iso(), note_id, user["id"]),
        )
        connection.execute(
            "DELETE FROM deleted_items WHERE account_id=? AND entity_type='note' AND entity_id=?",
            (user["id"], note_id),
        )
        record_change(connection, user["id"], "note", note_id, "upsert")
        row = connection.execute(
            "SELECT * FROM notes WHERE id=? AND account_id=?", (note_id, user["id"])
        ).fetchone()
    return public_note(row)


# --------------------------------------------------------------------------
# 增量同步
# --------------------------------------------------------------------------
@app.get("/api/v1/sync")
def sync(
    cursor: int = Query(default=0, ge=0),
    user: sqlite3.Row = Depends(current_user),
) -> dict[str, Any]:
    """游标式增量同步。

    客户端必须处理成功后才保存 nextCursor；解析失败保留旧游标，避免跳过数据。
    """
    account_id = user["id"]
    with db() as connection:
        changes = connection.execute(
            "SELECT seq, entity_type, entity_id, operation FROM sync_changes "
            "WHERE account_id=? AND seq>? ORDER BY seq",
            (account_id, cursor),
        ).fetchall()

        event_ids = {c["entity_id"] for c in changes if c["entity_type"] == "event"}
        note_ids = {c["entity_id"] for c in changes if c["entity_type"] == "note"}

        changed_events: list[dict[str, Any]] = []
        deleted_event_ids: list[str] = []
        for entity_id in event_ids:
            row = connection.execute(
                "SELECT * FROM events WHERE id=? AND account_id=?", (entity_id, account_id)
            ).fetchone()
            if row is None or row["deleted_at"]:
                deleted_event_ids.append(entity_id)
            else:
                changed_events.append(public_event(row))

        changed_notes: list[dict[str, Any]] = []
        deleted_note_ids: list[str] = []
        for entity_id in note_ids:
            row = connection.execute(
                "SELECT * FROM notes WHERE id=? AND account_id=?", (entity_id, account_id)
            ).fetchone()
            if row is None or row["deleted_at"]:
                deleted_note_ids.append(entity_id)
            else:
                changed_notes.append(public_note(row))

        next_cursor = current_cursor(connection, account_id)

    changed_events.sort(key=lambda item: item["start_at"])
    changed_notes.sort(key=lambda item: (not item["is_pinned"], item["updated_at"]), reverse=True)

    return {
        "nextCursor": next_cursor,
        "cursor": cursor,
        "changedEvents": changed_events,
        "deletedEventIds": deleted_event_ids,
        "changedNotes": changed_notes,
        "deletedNoteIds": deleted_note_ids,
        "serverTime": now_iso(),
        "hasMore": False,
    }


# --------------------------------------------------------------------------
# 同源前端托管（可选）
# --------------------------------------------------------------------------
# 让后端在 / 直接托管已构建的前端单文件（desktop/dist/index.html），
# 这样手机/电脑浏览器打开 http://<本机IP>:8123/ 即为完整客户端，
# 与 API 同源，无需处理 CORS，手机端可立即作为同步客户端使用。
# 仅当 desktop/dist 存在时启用；缺失则后端退化为纯 API 服务。
DIST_DIR = Path(__file__).resolve().parent.parent.parent / "desktop" / "dist"
_INDEX_FILE = DIST_DIR / "index.html"


@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="接口不存在")
    candidate = DIST_DIR / full_path
    no_store = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if candidate.is_file():
        return FileResponse(candidate, headers=no_store)
    if _INDEX_FILE.is_file():
        return FileResponse(_INDEX_FILE, headers=no_store)
    raise HTTPException(status_code=404, detail="未找到前端资源，请先构建 desktop 前端")


@app.get("/")
async def spa_root() -> FileResponse:
    if not _INDEX_FILE.is_file():
        raise HTTPException(status_code=404, detail="未找到前端资源，请先构建 desktop 前端")
    return FileResponse(_INDEX_FILE, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
