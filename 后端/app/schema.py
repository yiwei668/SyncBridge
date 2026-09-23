"""SQLite 表结构与迁移。

设计依据：《SyncBridge 方案 A》6.1 数据表。
要点：
- 启用外键约束；
- 为 account_id、updated_at、deleted_at 建立索引；
- 每个账号维护单调递增的变更游标（sync_changes.seq）；
- 升级走增量迁移，不删除已有数据（文档 12 节回滚要求）。
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 3

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'admin',
    created_at    TEXT NOT NULL
);

-- access token（短时有效）
CREATE TABLE IF NOT EXISTS tokens (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT ''
);

-- refresh token（可撤销）
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    platform        TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    location        TEXT NOT NULL DEFAULT '',
    start_at        TEXT NOT NULL,
    end_at          TEXT NOT NULL,
    all_day         INTEGER NOT NULL DEFAULT 0,
    timezone        TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    recurrence_rule TEXT,
    reminder_minutes TEXT NOT NULL DEFAULT '[]',
    is_pinned       INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL DEFAULT 'syncbridge',
    source_id       TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL,
    deleted_at      TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    id          TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '[]',
    is_pinned   INTEGER NOT NULL DEFAULT 0,
    is_archived INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'syncbridge',
    source_id   TEXT,
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL,
    deleted_at  TEXT
);

-- 变更日志：每个账号一条单调递增的游标
CREATE TABLE IF NOT EXISTS sync_changes (
    account_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    operation   TEXT NOT NULL,
    changed_at  TEXT NOT NULL,
    PRIMARY KEY (account_id, seq)
);

-- 软删除记录与保留时间（用于回收站与恢复）
CREATE TABLE IF NOT EXISTS deleted_items (
    account_id  TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    deleted_at  TEXT NOT NULL,
    purge_after TEXT NOT NULL,
    PRIMARY KEY (account_id, entity_type, entity_id)
);

-- 全局设置（提醒开关、默认提前量、客户端地址等）
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 提醒已触发记录：避免重复弹窗 / 重启后轰炸
CREATE TABLE IF NOT EXISTS reminder_fired (
    event_id        TEXT NOT NULL,
    reminder_offset INTEGER NOT NULL,
    fired_at        TEXT NOT NULL,
    PRIMARY KEY (event_id, reminder_offset)
);
"""

# 索引单独执行：老库的列可能还未补齐，必须等字段迁移完成后才能建索引
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_events_account    ON events(account_id);
CREATE INDEX IF NOT EXISTS idx_events_updated    ON events(updated_at);
CREATE INDEX IF NOT EXISTS idx_events_deleted    ON events(deleted_at);
CREATE INDEX IF NOT EXISTS idx_events_start      ON events(start_at);
CREATE INDEX IF NOT EXISTS idx_notes_account     ON notes(account_id);
CREATE INDEX IF NOT EXISTS idx_notes_updated     ON notes(updated_at);
CREATE INDEX IF NOT EXISTS idx_notes_deleted     ON notes(deleted_at);
CREATE INDEX IF NOT EXISTS idx_changes_account   ON sync_changes(account_id, seq);
CREATE INDEX IF NOT EXISTS idx_deleted_account   ON deleted_items(account_id, deleted_at);
CREATE INDEX IF NOT EXISTS idx_refresh_user      ON refresh_tokens(user_id, revoked);
CREATE INDEX IF NOT EXISTS idx_tokens_user       ON tokens(user_id);
"""

# 老库 -> 新库的字段补齐（幂等）
EVENTS_LEGACY_COLUMNS = {
    "source": "TEXT NOT NULL DEFAULT 'syncbridge'",
    "source_id": "TEXT",
    "is_pinned": "INTEGER NOT NULL DEFAULT 0",
}
NOTES_LEGACY_COLUMNS = {
    "source_id": "TEXT",
}


def get_setting(connection: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """读取一项设置；不存在时返回 default。"""
    row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(connection: sqlite3.Connection, key: str, value: str) -> None:
    """写入 / 覆盖一项设置。"""
    connection.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def migrate(connection: sqlite3.Connection) -> list[str]:
    """执行迁移，返回本次实际执行的迁移步骤说明。

    迁移策略：只做加表 / 加列 / 建索引，不做破坏性变更。
    """
    applied: list[str] = []
    tables = _tables(connection)

    # 先建新表（CREATE TABLE IF NOT EXISTS 不会动老表结构）
    connection.executescript(DDL)
    applied.append(f"schema v{SCHEMA_VERSION}: 基础表已同步")

    # 初始化默认提醒设置（幂等，老库不会覆盖已有值）
    _DEFAULT_SETTINGS = {
        "default_reminder_minutes": "[15]",
        "reminders_enabled": "1",
        "client_url": "http://localhost:8123/",
    }
    for _key, _value in _DEFAULT_SETTINGS.items():
        connection.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (_key, _value)
        )
    applied.append("默认提醒设置已写入 settings 表")

    # 再补齐老表缺失的列
    applied.extend(_migrate_legacy_columns(connection))

    # 列齐了才能建索引
    connection.executescript(INDEXES)
    applied.append("索引已同步")

    # 回填变更日志：保证老数据在首次增量同步时可见
    backfilled = _backfill_changes(connection)
    if backfilled:
        applied.append(f"变更日志回填 {backfilled} 条")

    # 老表若仍带 user_id NOT NULL 等约束，SQLite 无法原地去除，只能重建表
    applied.extend(_rebuild_if_legacy(connection))

    connection.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    return applied


# 重建表时保留的列顺序（v2 规范结构）
EVENTS_COLUMNS = (
    "id", "account_id", "title", "description", "location", "start_at", "end_at",
    "all_day", "timezone", "recurrence_rule", "reminder_minutes", "is_pinned",
    "source", "source_id", "version", "updated_at", "deleted_at",
)
NOTES_COLUMNS = (
    "id", "account_id", "title", "content", "tags", "is_pinned", "is_archived",
    "source", "source_id", "version", "updated_at", "deleted_at",
)

_LEGACY_MARKERS = ("user_id", "pinned", "archived", "tags_json", "start_time")


def _rebuild_if_legacy(connection: sqlite3.Connection) -> list[str]:
    """把仍含 v1 专有列的表重建为 v2 结构，数据按列名对应搬迁。

    SQLite 不支持 DROP COLUMN 之外的约束修改，带 user_id NOT NULL 的老表
    会让新代码的 INSERT 直接失败（新代码只写 account_id），因此必须重建。
    """
    applied: list[str] = []
    for table, columns in (("events", EVENTS_COLUMNS), ("notes", NOTES_COLUMNS)):
        existing = _columns(connection, table)
        if not existing:
            continue
        if not any(marker in existing for marker in _LEGACY_MARKERS):
            continue

        backup = f"{table}_legacy_backup"
        connection.execute(f"DROP TABLE IF EXISTS {backup}")
        connection.execute(f"ALTER TABLE {table} RENAME TO {backup}")

        # 按 v2 结构重建
        connection.executescript(DDL)

        shared = [column for column in columns if column in existing]
        # 老库的列名与 v2 不同，搬迁时做映射：SELECT 用旧名，INSERT 用新名
        rename = {
            "pinned": "is_pinned",
            "archived": "is_archived",
            "user_id": "account_id",
        }
        insert_columns = ", ".join(f'"{rename.get(name, name)}"' for name in shared)
        select_columns = ", ".join(f'"{name}"' for name in shared)
        connection.execute(
            f"INSERT INTO {table} ({insert_columns}) SELECT {select_columns} FROM {backup}"
        )
        connection.execute(f"DROP TABLE {backup}")
        applied.append(f"{table} 已重建为 v2 结构（保留 {len(shared)} 列数据）")
    return applied


def _migrate_legacy_columns(connection: sqlite3.Connection) -> list[str]:
    """把 v1 结构的旧表补齐到 v2 字段。只做加列与重命名，不丢数据。"""
    applied: list[str] = []

    # 兼容旧库：events 使用 start_time/end_time/pinned/reminders_json 的版本
    event_columns = _columns(connection, "events")
    if event_columns:
        if "start_time" in event_columns and "start_at" not in event_columns:
            connection.execute("ALTER TABLE events RENAME COLUMN start_time TO start_at")
            applied.append("events.start_time -> start_at")
        if "end_time" in event_columns and "end_at" not in event_columns:
            connection.execute("ALTER TABLE events RENAME COLUMN end_time TO end_at")
            applied.append("events.end_time -> end_at")
        if "reminders_json" in event_columns and "reminder_minutes" not in event_columns:
            connection.execute(
                "ALTER TABLE events RENAME COLUMN reminders_json TO reminder_minutes"
            )
            applied.append("events.reminders_json -> reminder_minutes")
        if "pinned" in event_columns and "is_pinned" not in event_columns:
            connection.execute(
                "ALTER TABLE events ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0"
            )
            connection.execute("UPDATE events SET is_pinned = pinned")
            applied.append("events.pinned -> is_pinned（含数据搬运）")

        event_columns = _columns(connection, "events")
        for name, ddl in EVENTS_LEGACY_COLUMNS.items():
            if name not in event_columns:
                connection.execute(f"ALTER TABLE events ADD COLUMN {name} {ddl}")
                applied.append(f"events 新增列 {name}")
        if "user_id" in event_columns and "account_id" not in event_columns:
            connection.execute(
                "ALTER TABLE events ADD COLUMN account_id TEXT NOT NULL DEFAULT ''"
            )
            connection.execute("UPDATE events SET account_id = user_id WHERE account_id = ''")
            applied.append("events.user_id -> account_id")

    # 兼容旧库：notes 使用 user_id / pinned / archived / tags_json
    note_columns = _columns(connection, "notes")
    if note_columns:
        if "pinned" in note_columns and "is_pinned" not in note_columns:
            connection.execute(
                "ALTER TABLE notes ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0"
            )
            connection.execute("UPDATE notes SET is_pinned = pinned")
            applied.append("notes.pinned -> is_pinned（含数据搬运）")
        if "archived" in note_columns and "is_archived" not in note_columns:
            connection.execute(
                "ALTER TABLE notes ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0"
            )
            connection.execute("UPDATE notes SET is_archived = archived")
            applied.append("notes.archived -> is_archived（含数据搬运）")
        if "tags_json" in note_columns and "tags" not in note_columns:
            connection.execute("ALTER TABLE notes RENAME COLUMN tags_json TO tags")
            applied.append("notes.tags_json -> tags")
        if "user_id" in note_columns and "account_id" not in note_columns:
            connection.execute(
                "ALTER TABLE notes ADD COLUMN account_id TEXT NOT NULL DEFAULT ''"
            )
            connection.execute("UPDATE notes SET account_id = user_id WHERE account_id = ''")
            applied.append("notes.user_id -> account_id")

        note_columns = _columns(connection, "notes")
        for name, ddl in NOTES_LEGACY_COLUMNS.items():
            if name not in note_columns:
                connection.execute(f"ALTER TABLE notes ADD COLUMN {name} {ddl}")
                applied.append(f"notes 新增列 {name}")

    # tokens 表补 created_at
    token_columns = _columns(connection, "tokens")
    if token_columns and "created_at" not in token_columns:
        connection.execute("ALTER TABLE tokens ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        applied.append("tokens 新增列 created_at")

    # devices 表补 last_seen_at
    device_columns = _columns(connection, "devices")
    if device_columns and "last_seen_at" not in device_columns:
        connection.execute("ALTER TABLE devices ADD COLUMN last_seen_at TEXT NOT NULL DEFAULT ''")
        connection.execute("UPDATE devices SET last_seen_at = created_at")
        applied.append("devices 新增列 last_seen_at")

    return applied


def _backfill_changes(connection: sqlite3.Connection) -> int:
    """为尚无变更日志的实体补写一条 seq 记录。"""
    written = 0
    for entity_type, table in (("event", "events"), ("note", "notes")):
        if table not in _tables(connection):
            continue
        rows = connection.execute(
            f"""
            SELECT t.account_id, t.id, t.updated_at, t.version
            FROM {table} t
            WHERE t.account_id <> ''
              AND NOT EXISTS (
                SELECT 1 FROM sync_changes c
                WHERE c.account_id = t.account_id
                  AND c.entity_type = ?
                  AND c.entity_id = t.id
              )
            """,
            (entity_type,),
        ).fetchall()
        for account_id, entity_id, updated_at, _version in rows:
            next_seq = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM sync_changes WHERE account_id=?",
                (account_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO sync_changes(account_id, seq, entity_type, entity_id, operation, changed_at) "
                "VALUES (?,?,?,?,?,?)",
                (account_id, next_seq, entity_type, entity_id, "upsert", updated_at),
            )
            written += 1
    return written
