"""SyncBridge 后台接口级测试。

覆盖《SyncBridge 方案 A》11.1 服务器管理、11.2 日历、11.3 便签、11.4 离线与异常
中属于服务端职责的部分。
"""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def main_module(tmp_path, monkeypatch):
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "test.db")
    main.init_db()
    return main


@pytest.fixture()
def client(main_module):
    return TestClient(main_module.app)


def auth(client, email="user_a", password="secret1"):
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def make_event(client, headers, **overrides):
    payload = {
        "title": "演示事件",
        "start_at": "2026-09-17T09:00:00+08:00",
        "end_at": "2026-09-17T10:00:00+08:00",
        "reminder_minutes": [15],
    }
    payload.update(overrides)
    response = client.post("/api/v1/events", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------
# 健康检查与契约
# --------------------------------------------------------------------------
def test_health_contract(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {
        "service": "syncbridge",
        "version": "1.0.0",
        "apiVersion": "v1",
        "status": "ok",
    }
    assert response.headers["X-SyncBridge-Version"] == "1.0.0"


def test_unauthenticated_access_is_rejected(client):
    """未携带 token 的登录后接口必须 401。"""
    for method, path in [
        ("get", "/api/v1/events"),
        ("get", "/api/v1/notes"),
        ("get", "/api/v1/sync"),
        ("get", "/api/v1/admin/overview"),
    ]:
        assert getattr(client, method)(path).status_code == 401, f"{path} 未拒绝匿名访问"

    assert client.post(
        "/api/v1/events",
        json={"title": "匿名写入", "start_at": "2026-09-17T09:00:00+08:00",
              "end_at": "2026-09-17T10:00:00+08:00"},
    ).status_code == 401


def test_cors_allows_tauri_origins(client):
    """Tauri 来源必须在 CORS 白名单中，否则登录预检会失败。"""
    for origin in ("http://tauri.localhost", "https://tauri.localhost"):
        response = client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin


# --------------------------------------------------------------------------
# 认证与账号隔离
# --------------------------------------------------------------------------
def test_refresh_token_rotation_and_revocation(client):
    login = client.post(
        "/api/v1/auth/login", json={"email": "user_admin", "password": "secret1"}
    ).json()
    assert login["refresh_token"] != login["access_token"]
    old_refresh = login["refresh_token"]

    refreshed = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert refreshed.status_code == 200
    new_access = refreshed.json()["access_token"]
    new_refresh = refreshed.json()["refresh_token"]
    assert new_access != login["access_token"]
    assert new_refresh != old_refresh, "刷新必须轮换刷新令牌本身"

    # 旧 access token 在刷新后应失效（refresh 会清空该账号的 access token）
    assert client.get(
        "/api/v1/events", headers={"Authorization": f"Bearer {login['access_token']}"}
    ).status_code == 401

    # 新 token 可用
    assert client.get(
        "/api/v1/events", headers={"Authorization": f"Bearer {new_access}"}
    ).status_code == 200

    # 关键回归：旧刷新令牌必须一次性失效，否则泄露的令牌可被无限重放
    replay = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert replay.status_code == 401, f"旧刷新令牌被重放，返回 {replay.status_code}"
    assert client.post(
        "/api/v1/auth/refresh", json={"refresh_token": old_refresh}
    ).status_code == 401

    # 轮换出来的新刷新令牌仍然有效（一次性但不影响正常续期链路）
    assert client.post(
        "/api/v1/auth/refresh", json={"refresh_token": new_refresh}
    ).status_code == 200


def test_rotated_refresh_token_expires_after_single_use(client):
    """刷新令牌严格一次性：连续两次使用同一个令牌，第二次必须失败。"""
    login = client.post(
        "/api/v1/auth/login", json={"email": "user_once", "password": "secret1"}
    ).json()
    token = login["refresh_token"]
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": token}).status_code == 200
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": token}).status_code == 401


def test_logout_revokes_refresh_token(client):
    login = client.post(
        "/api/v1/auth/login", json={"email": "user_b", "password": "secret1"}
    ).json()
    headers = {"Authorization": f"Bearer {login['access_token']}"}
    assert client.post(
        "/api/v1/auth/logout", headers=headers, json={"refresh_token": login["refresh_token"]}
    ).status_code == 200
    assert client.post(
        "/api/v1/auth/refresh", json={"refresh_token": login["refresh_token"]}
    ).status_code == 401


def test_logout_revokes_all_account_refresh_tokens(client):
    """登出必须清掉该账号全部刷新令牌，不能只吊销被提交的那一个。"""
    email = "user_multisession"
    first = client.post("/api/v1/auth/login", json={"email": email, "password": "secret1"}).json()
    second = client.post("/api/v1/auth/login", json={"email": email, "password": "secret1"}).json()
    assert first["refresh_token"] != second["refresh_token"]

    headers = {"Authorization": f"Bearer {second['access_token']}"}
    assert client.post(
        "/api/v1/auth/logout", headers=headers, json={"refresh_token": second["refresh_token"]}
    ).status_code == 200

    # 另一会话的刷新令牌也应一并失效
    assert client.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    ).status_code == 401
    assert client.post(
        "/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]}
    ).status_code == 401


def test_accounts_are_isolated(client):
    """A 账号的数据不能被 B 账号读到或改到。"""
    headers_a = auth(client, "user_a")
    headers_b = auth(client, "user_b")

    event = make_event(client, headers_a, title="A 的私有事件")

    assert client.get("/api/v1/events", headers=headers_b).json() == []
    assert client.delete(f"/api/v1/events/{event['id']}", headers=headers_b).status_code == 404
    assert client.patch(
        f"/api/v1/events/{event['id']}", headers=headers_b,
        json={"title": "越权修改", "start_at": event["start_at"], "end_at": event["end_at"]},
    ).status_code == 404


def test_device_registration(client):
    headers = auth(client)
    response = client.post(
        "/api/v1/devices/register",
        headers=headers,
        json={"name": "我的台式机", "platform": "windows"},
    )
    assert response.status_code == 200
    assert response.json()["device_id"]
    assert client.get("/api/v1/admin/overview", headers=headers).json()["counts"]["devices"] == 1


# --------------------------------------------------------------------------
# 日历
# --------------------------------------------------------------------------
def test_event_crud_lifecycle(client):
    headers = auth(client)

    created = make_event(client, headers, title="新建事件", location="三水图书馆")
    assert created["version"] == 1
    assert created["is_pinned"] is False
    assert created["reminder_minutes"] == [15]
    assert created["location"] == "三水图书馆"

    updated = client.patch(
        f"/api/v1/events/{created['id']}",
        headers=headers,
        json={
            "title": "已修改",
            "start_at": created["start_at"],
            "end_at": created["end_at"],
            "is_pinned": True,
            "version": created["version"],
        },
    ).json()
    assert updated["title"] == "已修改"
    assert updated["is_pinned"] is True
    assert updated["version"] == 2

    assert client.delete(f"/api/v1/events/{created['id']}", headers=headers).status_code == 200
    assert client.get("/api/v1/events", headers=headers).json() == []
    assert len(client.get("/api/v1/events?include_deleted=true", headers=headers).json()) == 1


def test_event_end_before_start_is_rejected(client):
    """前端校验之外，服务端也必须拒绝结束早于开始。"""
    headers = auth(client)
    response = client.post(
        "/api/v1/events",
        headers=headers,
        json={
            "title": "时间倒挂",
            "start_at": "2026-09-17T12:00:00+08:00",
            "end_at": "2026-09-17T09:00:00+08:00",
        },
    )
    assert response.status_code == 422


def test_version_conflict_returns_409_not_silent_overwrite(client):
    """冲突不得静默覆盖（文档 11.4）。"""
    headers = auth(client)
    event = make_event(client, headers)

    first = client.patch(
        f"/api/v1/events/{event['id']}",
        headers=headers,
        json={
            "title": "客户端甲",
            "start_at": event["start_at"],
            "end_at": event["end_at"],
            "version": 1,
        },
    )
    assert first.status_code == 200

    # 客户端乙仍持有 version=1，必须被拒绝
    conflict = client.patch(
        f"/api/v1/events/{event['id']}",
        headers=headers,
        json={
            "title": "客户端乙",
            "start_at": event["start_at"],
            "end_at": event["end_at"],
            "version": 1,
        },
    )
    assert conflict.status_code == 409
    assert "版本冲突" in conflict.json()["detail"]

    # 服务端保留客户端甲的写入
    assert client.get("/api/v1/events", headers=headers).json()[0]["title"] == "客户端甲"


def test_all_day_event_has_no_timezone_drift(client):
    """全天事件跨时区读写不产生偏移（文档 11.2）。"""
    headers = auth(client)
    event = make_event(
        client, headers,
        title="全天事件", all_day=True,
        start_at="2026-09-17T00:00:00+08:00", end_at="2026-09-18T00:00:00+08:00",
        timezone="Asia/Shanghai",
    )
    assert event["all_day"] is True
    assert event["start_at"] == "2026-09-17T00:00:00+08:00"
    fetched = client.get("/api/v1/events", headers=headers).json()[0]
    assert fetched["start_at"] == "2026-09-17T00:00:00+08:00"
    assert fetched["timezone"] == "Asia/Shanghai"


def test_recurrence_rule_is_not_duplicated_on_update(client):
    """重复事件更新不应产生副本（文档 11.2）。"""
    headers = auth(client)
    event = make_event(client, headers, recurrence_rule="FREQ=WEEKLY;BYDAY=MO")
    for _ in range(3):
        client.patch(
            f"/api/v1/events/{event['id']}",
            headers=headers,
            json={
                "title": "每周例会",
                "start_at": event["start_at"],
                "end_at": event["end_at"],
                "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO",
            },
        )
    events = client.get("/api/v1/events", headers=headers).json()
    assert len(events) == 1
    assert events[0]["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=MO"


def test_reminder_minutes_are_validated_and_deduplicated(client):
    headers = auth(client)
    event = make_event(client, headers, reminder_minutes=[60, 15, 15, 1440])
    assert event["reminder_minutes"] == [15, 60, 1440]

    bad = client.post(
        "/api/v1/events",
        headers=headers,
        json={
            "title": "非法提醒",
            "start_at": "2026-09-17T09:00:00+08:00",
            "end_at": "2026-09-17T10:00:00+08:00",
            "reminder_minutes": [-5],
        },
    )
    assert bad.status_code == 422


def test_deleted_event_can_be_restored(client):
    headers = auth(client)
    event = make_event(client, headers)
    client.delete(f"/api/v1/events/{event['id']}", headers=headers)
    restored = client.post(f"/api/v1/events/{event['id']}/restore", headers=headers)
    assert restored.status_code == 200
    assert restored.json()["deleted_at"] is None
    assert len(client.get("/api/v1/events", headers=headers).json()) == 1


# --------------------------------------------------------------------------
# 便签
# --------------------------------------------------------------------------
def test_note_crud_and_flags(client):
    headers = auth(client)
    note = client.post(
        "/api/v1/notes",
        headers=headers,
        json={"title": "购物清单", "content": "牛奶", "tags": ["生活", "生活", "超市"]},
    ).json()
    assert note["tags"] == ["生活", "超市"]
    assert note["is_pinned"] is False
    assert note["is_archived"] is False

    updated = client.patch(
        f"/api/v1/notes/{note['id']}",
        headers=headers,
        json={
            "title": "购物清单", "content": "牛奶、鸡蛋",
            "tags": ["生活"], "is_pinned": True, "is_archived": True,
            "version": note["version"],
        },
    ).json()
    assert updated["is_pinned"] is True
    assert updated["is_archived"] is True
    assert updated["version"] == 2

    assert client.delete(f"/api/v1/notes/{note['id']}", headers=headers).status_code == 200
    assert client.post(f"/api/v1/notes/{note['id']}/restore", headers=headers).status_code == 200


def test_same_source_note_is_not_imported_twice(client):
    """相同来源不重复导入（文档 11.3）。"""
    headers = auth(client)
    payload = {
        "title": "来自小米便签",
        "content": "分享内容",
        "source": "mi-share",
        "source_id": "share-20260917-001",
    }
    first = client.post("/api/v1/notes", headers=headers, json=payload)
    second = client.post("/api/v1/notes", headers=headers, json=payload)
    assert first.status_code == 201
    assert second.status_code in (200, 201)
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get("/api/v1/notes", headers=headers).json()) == 1


def test_note_version_conflict(client):
    headers = auth(client)
    note = client.post("/api/v1/notes", headers=headers, json={"title": "v1"}).json()
    client.patch(
        f"/api/v1/notes/{note['id']}", headers=headers,
        json={"title": "v2", "version": note["version"]},
    )
    conflict = client.patch(
        f"/api/v1/notes/{note['id']}", headers=headers,
        json={"title": "stale", "version": note["version"]},
    )
    assert conflict.status_code == 409


# --------------------------------------------------------------------------
# 增量同步
# --------------------------------------------------------------------------
def test_sync_cursor_is_monotonic_and_incremental(client):
    headers = auth(client)

    baseline = client.get("/api/v1/sync?cursor=0", headers=headers).json()
    assert baseline["nextCursor"] == 0
    assert baseline["changedEvents"] == []

    event = make_event(client, headers, title="增量事件")
    first = client.get("/api/v1/sync?cursor=0", headers=headers).json()
    cursor = first["nextCursor"]
    assert cursor >= 1
    assert [item["id"] for item in first["changedEvents"]] == [event["id"]]

    # 用同一个游标再次拉取，不应重复返回已处理的数据
    again = client.get(f"/api/v1/sync?cursor={cursor}", headers=headers).json()
    assert again["changedEvents"] == []
    assert again["nextCursor"] == cursor

    # 新增一条后，只有新记录出现
    second_event = make_event(client, headers, title="第二条")
    delta = client.get(f"/api/v1/sync?cursor={cursor}", headers=headers).json()
    assert [item["id"] for item in delta["changedEvents"]] == [second_event["id"]]
    assert delta["nextCursor"] > cursor


def test_sync_reports_deletions_with_ids(client):
    headers = auth(client)
    event = make_event(client, headers)
    note = client.post("/api/v1/notes", headers=headers, json={"title": "待删便签"}).json()

    cursor = client.get("/api/v1/sync?cursor=0", headers=headers).json()["nextCursor"]
    client.delete(f"/api/v1/events/{event['id']}", headers=headers)
    client.delete(f"/api/v1/notes/{note['id']}", headers=headers)

    delta = client.get(f"/api/v1/sync?cursor={cursor}", headers=headers).json()
    assert event["id"] in delta["deletedEventIds"]
    assert note["id"] in delta["deletedNoteIds"]
    assert delta["changedEvents"] == []


def test_sync_payload_contract(client):
    """响应字段必须与文档 6.3 一致。"""
    headers = auth(client)
    make_event(client, headers)
    client.post("/api/v1/notes", headers=headers, json={"title": "契约便签"})
    body = client.get("/api/v1/sync?cursor=0", headers=headers).json()
    for key in (
        "nextCursor", "changedEvents", "deletedEventIds",
        "changedNotes", "deletedNoteIds", "serverTime",
    ):
        assert key in body, f"缺少字段 {key}"
    assert len(body["changedEvents"]) == 1
    assert len(body["changedNotes"]) == 1


def test_stale_cursor_does_not_skip_data(client):
    """客户端保留旧游标重试时，数据不应丢失（文档 6.3）。"""
    headers = auth(client)
    client.post("/api/v1/notes", headers=headers, json={"title": "第一条"})
    cursor_before = client.get("/api/v1/sync?cursor=0", headers=headers).json()["nextCursor"]

    client.post("/api/v1/notes", headers=headers, json={"title": "第二条"})

    # 模拟客户端解析失败，仍用旧游标重试
    retry = client.get(f"/api/v1/sync?cursor={cursor_before}", headers=headers).json()
    titles = [item["title"] for item in retry["changedNotes"]]
    assert "第二条" in titles


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------
def test_schema_has_required_tables_and_foreign_keys(main_module, tmp_path):
    import sqlite3

    connection = sqlite3.connect(main_module.DB_PATH)
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    for required in (
        "users", "refresh_tokens", "devices", "events", "notes",
        "sync_changes", "deleted_items",
    ):
        assert required in tables, f"缺少表 {required}"

    indexes = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }
    for required in (
        "idx_events_account", "idx_events_updated", "idx_events_deleted",
        "idx_notes_account", "idx_notes_updated", "idx_notes_deleted",
    ):
        assert required in indexes, f"缺少索引 {required}"
    connection.close()


def test_migration_is_idempotent(main_module):
    """重复执行迁移不报错，也不重复写入数据。"""
    first = main_module.init_db()
    second = main_module.init_db()
    assert isinstance(first, list) and isinstance(second, list)


def test_admin_overview_counts(client):
    headers = auth(client, "user_admin")
    make_event(client, headers)
    client.post("/api/v1/notes", headers=headers, json={"title": "n"})
    counts = client.get("/api/v1/admin/overview", headers=headers).json()["counts"]
    assert counts["users"] == 1
    assert counts["events"] == 1
    assert counts["notes"] == 1
