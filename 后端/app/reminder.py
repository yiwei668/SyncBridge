"""日历提醒调度器。

职责：随 FastAPI 启动的后台线程，按固定间隔轮询未删除事件，对每个
reminder_minutes 偏移计算触发时刻，到点在后端所在 Windows 电脑弹出系统通知。
- 时间一律按 UTC 比较（与 main.now_iso 一致），展示时转本机时区；
- reminder_fired 表保证同一次"到点窗口"内只弹一次、重启不重复弹；
- 错过窗口（now 远超 fire）不弹，避免历史事件重启轰炸。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import schema as schema_module

POLL_INTERVAL = 20  # 秒：轮询间隔
GRACE = 120  # 秒：触发窗口宽度，超过则视为已错过

_DB_PATH = Path(
    os.getenv(
        "SyncBridge_DB_PATH",
        Path(__file__).resolve().parent.parent / "syncbridge.db",
    )
)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# --------------------------------------------------------------------------
# 时间解析
# --------------------------------------------------------------------------
def _resolve_zone(tz_name: str | None):
    """把时区名解析为 datetime.tzinfo；失败返回 None。"""
    if not tz_name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)
    except Exception:
        # Windows 缺 tzdata 时降级到固定偏移
        mapping = {"Asia/Shanghai": 28800, "UTC": 0}
        if tz_name in mapping:
            return timezone(timedelta(seconds=mapping[tz_name]))
        return None


def parse_event_start_utc(start_at: str, tz_name: str | None) -> datetime | None:
    """把事件的 start_at 解析为带 UTC 的 datetime。

    兼容两种客户端格式：
    1) 带时区信息（如 2026-09-21T21:00:00+08:00 或 ...Z）；
    2) naive（如 2026-09-21T21:00:00），按 tz_name 声明的时区解释。
    无法解析返回 None（该事件被跳过）。
    """
    raw = (start_at or "").strip()
    if not raw:
        return None
    text = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        zone = _resolve_zone(tz_name)
        if zone is None:
            return None
        try:
            dt = dt.replace(tzinfo=zone)
        except Exception:
            return None
    return dt.astimezone(timezone.utc)


def _local_zone():
    try:
        return datetime.now().astimezone().tzinfo
    except Exception:
        return timezone.utc


# --------------------------------------------------------------------------
# reminder_minutes 解析
# --------------------------------------------------------------------------
def _parse_reminder_list(raw: str | None) -> list[int]:
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [int(x) for x in data if isinstance(x, int) and 0 <= x <= 40320]


def _parse_default_reminder(env: str | None, db_value: str | None) -> list[int]:
    """解析全局默认提前提醒。env 可逗号分隔（'15,30'）或 JSON 数组。"""
    source = env if env else db_value
    try:
        data = json.loads(source) if source else []
    except Exception:
        data = []
    if isinstance(data, list):
        out = [int(x) for x in data if isinstance(x, int) and 0 <= x <= 40320]
        if out:
            return sorted(out)
    if env:
        out = []
        for part in env.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except ValueError:
                pass
        if out:
            return sorted(out)
    return [15]


# --------------------------------------------------------------------------
# reminder_fired 幂等
# --------------------------------------------------------------------------
def _already_fired(event_id: str, offset: int) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM reminder_fired WHERE event_id=? AND reminder_offset=?",
            (event_id, offset),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _mark_fired(event_id: str, offset: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO reminder_fired(event_id, reminder_offset, fired_at) VALUES (?,?,?)",
            (event_id, offset, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 调度器
# --------------------------------------------------------------------------
class ReminderScheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="reminder-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # 单轮异常不应终止调度
                print(f"[reminder] tick error: {exc}")
            self._stop.wait(POLL_INTERVAL)

    def _tick(self) -> None:
        from . import notifier

        conn = _connect()
        try:
            enabled = schema_module.get_setting(conn, "reminders_enabled", "1")
            if str(enabled) not in ("1", "true", "True"):
                return
            default = _parse_default_reminder(
                os.getenv("SyncBridge_DEFAULT_REMINDER"),
                schema_module.get_setting(conn, "default_reminder_minutes", "[15]"),
            )
            client_url = os.getenv("SyncBridge_CLIENT_URL") or schema_module.get_setting(
                conn, "client_url", "http://localhost:8123/"
            )
            rows = conn.execute(
                """SELECT id,title,description,location,start_at,timezone,reminder_minutes
                   FROM events WHERE deleted_at IS NULL"""
            ).fetchall()
        finally:
            conn.close()

        now = datetime.now(timezone.utc)
        for row in rows:
            try:
                self._check_event(row, now, default, client_url)
            except Exception as exc:
                print(f"[reminder] event check error: {exc}")

    def _check_event(
        self, row: sqlite3.Row, now: datetime, default: list[int], client_url: str
    ) -> None:
        start_utc = parse_event_start_utc(row["start_at"], row["timezone"])
        if start_utc is None:
            return
        offsets = _parse_reminder_list(row["reminder_minutes"]) or default
        for offset in offsets:
            fire = start_utc - timedelta(minutes=offset)
            if now < fire:
                continue  # 还没到点
            if now > fire + timedelta(seconds=GRACE):
                continue  # 已错过窗口，不弹（避免重启轰炸历史）
            if _already_fired(row["id"], offset):
                continue
            self._fire(row, fire, offset, client_url)
            _mark_fired(row["id"], offset)

    def _fire(
        self, row: sqlite3.Row, fire: datetime, offset: int, client_url: str
    ) -> None:
        from . import notifier

        local = fire.astimezone(_local_zone())
        when = local.strftime("%Y-%m-%d %H:%M")
        offset_text = "准点" if offset == 0 else f"提前 {offset} 分钟"
        title = f"⏰ {row['title']}"
        lines = [f"时间：{when}（{offset_text}）"]
        if row["location"]:
            lines.append(f"地点：{row['location']}")
        if row["description"]:
            desc = row["description"]
            if len(desc) > 80:
                desc = desc[:80] + "…"
            lines.append(f"备注：{desc}")
        message = "\n".join(lines)
        try:
            notifier.show_windows_toast(title, message, launch=client_url)
        except Exception as exc:
            print(f"[reminder] notify failed: {exc}")


_scheduler = ReminderScheduler()


def start_scheduler() -> None:
    """仅在 Windows 上启动调度器（通知只能在本机屏幕弹出）。"""
    if sys.platform == "win32":
        _scheduler.start()


def stop_scheduler() -> None:
    _scheduler.stop()
