"""端到端联调：模拟电脑端客户端的真实调用序列。

覆盖《SyncBridge 方案 A》11.2 日历、11.3 便签、11.4 离线与异常。
需要后端已在 127.0.0.1:8123 运行。
"""

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8123/api/v1"
passed = 0
failed = 0

# 本机回环请求不走系统代理，否则会被代理拦截返回 502
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    request.add_header("Accept", "application/json")
    if data:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with opener.open(request, timeout=10) as response:
            text = response.read().decode()
            return response.status, json.loads(text) if text else None
    except urllib.error.HTTPError as error:
        text = error.read().decode()
        try:
            return error.code, json.loads(text) if text else None
        except json.JSONDecodeError:
            return error.code, text


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label} {detail}")


print("=" * 68)
print("SyncBridge 端到端联调")
print("=" * 68)

# ---------------------------------------------------------------- 1 健康检查
print("\n[1] 健康检查与地址契约")
status, health = call("GET", "/health")
check("健康接口返回 200", status == 200, status)
check("service=syncbridge", health.get("service") == "syncbridge")
check("apiVersion=v1", health.get("apiVersion") == "v1")

# ---------------------------------------------------------------- 2 登录
print("\n[2] 登录与令牌")
stamp = str(int(time.time()))
email = f"e2e{stamp}"
status, login = call("POST", "/auth/login", body={"email": email, "password": "secret123"})
check("登录成功", status == 200, status)
token = login["access_token"]
refresh = login["refresh_token"]
check("同时下发 access 与 refresh 令牌", bool(token) and bool(refresh))
check("access 与 refresh 不同", token != refresh)

# ---------------------------------------------------------------- 3 匿名拒绝
print("\n[3] 匿名访问必须被拒绝")
status, _ = call("GET", "/events")
check("未带令牌读事件返回 401", status == 401, status)
status, _ = call("POST", "/events", body={"title": "x", "start_at": "a", "end_at": "b"})
check("未带令牌写事件返回 401", status == 401, status)

# ---------------------------------------------------------------- 4 设备注册
print("\n[4] 设备注册")
status, device = call(
    "POST", "/devices/register", token,
    body={"name": "我的台式机", "platform": "windows"},
)
check("设备注册成功", status == 200 and bool(device.get("device_id")), status)

# ---------------------------------------------------------------- 5 建立基线
print("\n[5] 建立同步基线")
status, baseline = call("GET", "/sync?cursor=0", token)
check("同步接口契约完整", all(
    key in baseline for key in (
        "nextCursor", "changedEvents", "deletedEventIds",
        "changedNotes", "deletedNoteIds", "serverTime",
    )
), list(baseline.keys()) if baseline else None)
check("初始游标为 0", baseline["nextCursor"] == 0, baseline["nextCursor"])

# ---------------------------------------------------------------- 6 新建事件
print("\n[6] 新建事件（含全天与提醒）")
status, event = call("POST", "/events", token, body={
    "title": "小组会议",
    "location": "三水图书馆 3 楼",
    "description": "带笔记本",
    "start_at": "2026-09-20T09:00:00+08:00",
    "end_at": "2026-09-20T10:30:00+08:00",
    "timezone": "Asia/Shanghai",
    "reminder_minutes": [15, 60],
    "is_pinned": True,
    "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO",
})
check("事件创建返回 201", status == 201, status)
check("初始 version=1", event["version"] == 1, event["version"])
check("地点保存正确", event["location"] == "三水图书馆 3 楼")
check("提醒去重排序", event["reminder_minutes"] == [15, 60], event["reminder_minutes"])
check("置顶状态保存", event["is_pinned"] is True)
check("重复规则保存", event["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=MO")

status, all_day = call("POST", "/events", token, body={
    "title": "国庆假期",
    "start_at": "2026-10-01T00:00:00+08:00",
    "end_at": "2026-10-08T00:00:00+08:00",
    "all_day": True,
})
check("全天事件无时区偏移", all_day["start_at"] == "2026-10-01T00:00:00+08:00", all_day["start_at"])

# ---------------------------------------------------------------- 7 时间校验
print("\n[7] 时间校验（服务端兜底）")
status, _ = call("POST", "/events", token, body={
    "title": "时间倒挂",
    "start_at": "2026-09-20T12:00:00+08:00",
    "end_at": "2026-09-20T09:00:00+08:00",
})
check("结束早于开始被拒绝 422", status == 422, status)

# ---------------------------------------------------------------- 8 增量同步
print("\n[8] 增量同步与游标推进")
status, first = call("GET", "/sync?cursor=0", token)
cursor = first["nextCursor"]
check("游标已推进", cursor > 0, cursor)
check("返回两条变更事件", len(first["changedEvents"]) == 2, len(first["changedEvents"]))

status, again = call("GET", f"/sync?cursor={cursor}", token)
check("同游标重复拉取无新数据", again["changedEvents"] == [], len(again["changedEvents"]))
check("游标保持单调不减", again["nextCursor"] >= cursor)

# ---------------------------------------------------------------- 9 版本冲突
print("\n[9] 版本冲突不得静默覆盖")
status, ok_edit = call("PATCH", f"/events/{event['id']}", token, body={
    "title": "客户端甲改名",
    "start_at": event["start_at"],
    "end_at": event["end_at"],
    "version": 1,
})
check("首次修改成功", status == 200, status)
check("version 递增到 2", ok_edit["version"] == 2, ok_edit["version"])

status, conflict = call("PATCH", f"/events/{event['id']}", token, body={
    "title": "客户端乙改名",
    "start_at": event["start_at"],
    "end_at": event["end_at"],
    "version": 1,
})
check("陈旧 version 返回 409", status == 409, status)
check("冲突信息含服务端版本", "版本冲突" in (conflict.get("detail") or ""), conflict)

status, current = call("GET", "/events", token)
target = next(item for item in current if item["id"] == event["id"])
check("服务端保留先写入的内容", target["title"] == "客户端甲改名", target["title"])

# ---------------------------------------------------------------- 10 便签
print("\n[10] 便签 CRUD 与来源去重")
status, note = call("POST", "/notes", token, body={
    "title": "购物清单",
    "content": "牛奶、鸡蛋",
    "tags": ["生活", "生活", "超市"],
})
check("便签创建成功", status == 201, status)
check("标签去重", note["tags"] == ["生活", "超市"], note["tags"])

status, dup1 = call("POST", "/notes", token, body={
    "title": "来自小米便签",
    "content": "分享内容",
    "source": "mi-share",
    "source_id": "share-001",
})
status, dup2 = call("POST", "/notes", token, body={
    "title": "来自小米便签",
    "content": "分享内容",
    "source": "mi-share",
    "source_id": "share-001",
})
check("相同来源不重复导入", dup1["id"] == dup2["id"], f"{dup1['id']} vs {dup2['id']}")

status, notes = call("GET", "/notes", token)
check("便签总数为 2", len(notes) == 2, len(notes))

# ---------------------------------------------------------------- 11 删除与恢复
print("\n[11] 软删除、删除上报与恢复")
status, before_delete = call("GET", "/sync?cursor=0", token)
mark = before_delete["nextCursor"]

status, _ = call("DELETE", f"/events/{event['id']}", token)
status, _ = call("DELETE", f"/notes/{note['id']}", token)

status, delta = call("GET", f"/sync?cursor={mark}", token)
check("删除事件通过 deletedEventIds 上报", event["id"] in delta["deletedEventIds"], delta["deletedEventIds"])
check("删除便签通过 deletedNoteIds 上报", note["id"] in delta["deletedNoteIds"], delta["deletedNoteIds"])
check("已删除项不出现在 changedEvents", all(
    item["id"] != event["id"] for item in delta["changedEvents"]
))

status, _ = call("POST", f"/events/{event['id']}/restore", token)
status, restored = call("GET", "/events", token)
check("恢复后事件重新可见", any(item["id"] == event["id"] for item in restored))

# ---------------------------------------------------------------- 12 账号隔离
print("\n[12] 账号隔离")
status, other = call("POST", "/auth/login", body={
    "email": f"other{stamp}", "password": "secret123",
})
other_token = other["access_token"]
status, other_events = call("GET", "/events", other_token)
check("另一账号看不到本账号事件", other_events == [], len(other_events))
status, _ = call("PATCH", f"/events/{event['id']}", other_token, body={
    "title": "越权", "start_at": event["start_at"], "end_at": event["end_at"],
})
check("另一账号修改被拒绝 404", status == 404, status)

# ---------------------------------------------------------------- 13 令牌刷新
print("\n[13] 令牌刷新")
status, refreshed = call("POST", "/auth/refresh", body={"refresh_token": refresh})
check("刷新成功", status == 200, status)
check("返回新的 access 令牌", refreshed["access_token"] != token)
status, _ = call("GET", "/events", {"Authorization": f"Bearer {token}"} and token)
status, _ = call("GET", "/events")
check("匿名仍被拒绝", status == 401, status)

status, after_refresh = call("GET", "/events", refreshed["access_token"])
check("新令牌可正常访问", status == 200, status)

# ---------------------------------------------------------------- 14 后台统计
print("\n[14] 后台数据统计")
status, overview = call("GET", "/admin/overview", refreshed["access_token"])
check("后台概览可读", status == 200, status)
check("统计包含事件数", overview["counts"]["events"] >= 2, overview.get("counts"))

print()
print("=" * 68)
print(f"结果：通过 {passed} 项，失败 {failed} 项")
print("=" * 68)
sys.exit(1 if failed else 0)
