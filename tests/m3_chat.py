"""M3 验收：流式对话 + 20 次原子配额 + 断流回滚 + 并发只放一个 + 心跳。

服务需以调试模式起：
    PORT=8020 DEBUG_MODE=1 FAKE_UPSTREAM=1 ./start.sh

假上游只认以 `#fake` 开头的消息；不带前缀的走真实 MiniMax，用来做一次端到端冒烟。
每个用例用一个新访客，避开 6 次/分钟的对话限流，也顺便验额度是否按人隔离。
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = os.environ.get("DA_BASE", "http://127.0.0.1:8020")
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "app.db"
TZ = timezone(timedelta(hours=8))

PASS, FAIL = [], []
_ip_seq = [0]


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'  OK ' if ok else '  !! '} {name}" + (f"  [{detail}]" if detail else ""))


def read_env() -> dict:
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


ADMIN_PW = read_env().get("ADMIN_PASSWORD", "")


def next_ip() -> str:
    _ip_seq[0] += 1
    return f"10.20.{_ip_seq[0] // 250}.{_ip_seq[0] % 250 + 1}"


def db_rows(sql: str, params=()) -> list[tuple]:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def db_exec(sql: str, params=()) -> None:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        # 显式禁用代理：本机环境设了 HTTP_PROXY，urllib 会拿它去代理 127.0.0.1 并返回 502
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar),
        )
        self.xff = next_ip()

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Forwarded-For", self.xff)
        try:
            with self.opener.open(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw

    def chat(self, task_id: str, text: str):
        """发一条消息，读完整个 SSE 流，返回 (状态码, [(event, data)], 原始文本)。"""
        req = urllib.request.Request(
            f"{BASE}/api/tasks/{task_id}/chat",
            data=json.dumps({"text": text}).encode(),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "text/event-stream")
        req.add_header("X-Forwarded-For", self.xff)
        try:
            resp = self.opener.open(req, timeout=120)
        except urllib.error.HTTPError as exc:
            return exc.code, [], exc.read().decode("utf-8", "replace")
        try:
            raw = resp.read().decode("utf-8", "replace")
        finally:
            resp.close()
        return 200, parse_sse(raw), raw


def parse_sse(raw: str) -> list[tuple[str, dict | None]]:
    events: list[tuple[str, dict | None]] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        name, payload = "message", None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                body = line[5:].strip()
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = {"raw": body}
        events.append((name, payload))
    return events


def new_guest(admin: Client, tag: str) -> tuple[Client, dict]:
    status, body = admin.call("POST", "/api/admin/invites",
                              {"count": 1, "max_uses": 1, "days": 7, "daily_quota": 3,
                               "note": f"m3-{tag}"})
    code = (body.get("codes") or [""])[0]
    guest = Client()
    guest.call("POST", "/api/join", {"code": code, "nickname": f"m3{tag}"[:20]})
    status, me = guest.call("GET", "/api/me")
    return guest, me


def new_task(guest: Client, tag: str) -> str:
    status, body = guest.call("POST", "/api/tasks",
                              {"mode": "writing", "goal": f"验收用例 {tag}"})
    return body.get("id") or body.get("task", {}).get("id") or ""


def quota_used(guest: Client) -> int:
    _, me = guest.call("GET", "/api/me")
    return int((me.get("quota") or {}).get("used", -1))


def main() -> int:
    if not DB_PATH.exists():
        print(f"找不到数据库 {DB_PATH}")
        return 2
    admin = Client()
    status, _ = admin.call("POST", "/api/admin/login", {"password": ADMIN_PW})
    if status != 200:
        print(f"管理员登录失败 {status}，检查 .env 的 ADMIN_PASSWORD")
        return 2

    print("\n[1] 正常流式（假上游）")
    guest, me = new_guest(admin, "A")
    task = new_task(guest, "A")
    check("建任务成功", bool(task), task)
    status, events, raw = guest.chat(task, '#fake {"chunks": 3, "delay_ms": 20}')
    names = [e[0] for e in events]
    check("HTTP 200", status == 200, f"got {status}")
    check("有 meta 事件", "meta" in names, str(names[:4]))
    check("有 thinking 增量", "thinking" in names)
    check("有 text 增量且共 3 段", names.count("text") == 3, f"text×{names.count('text')}")
    check("有 usage 事件", "usage" in names)
    check("以 done 收尾", names and names[-1] == "done", str(names[-3:]))
    usage = next((p for n, p in events if n == "usage"), {}) or {}
    check("usage 带真实 token", (usage.get("in") or 0) > 0 and (usage.get("out") or 0) > 0, str(usage))
    check("额度扣 1 次", quota_used(guest) == 1, f"used={quota_used(guest)}")

    print("\n[2] 并行再发一条 → 只放一个")
    guest2, _ = new_guest(admin, "B")
    task2 = new_task(guest2, "B")
    box: dict = {}

    def slow() -> None:
        box["result"] = guest2.chat(task2, '#fake {"chunks": 3, "delay_ms": 1500}')

    worker = threading.Thread(target=slow)
    worker.start()
    time.sleep(1.2)  # 让第一条真的开始跑
    status_b, body_b = guest2.call(
        "POST", f"/api/tasks/{task2}/chat", {"text": '#fake {"chunks": 1}'}
    )
    worker.join(timeout=60)
    check("第二条被拒为 409", status_b == 409, f"got {status_b} {body_b}")
    first_status, first_events, _ = box.get("result") or (0, [], "")
    check("第一条仍正常跑完", first_status == 200 and any(n == "done" for n, _ in first_events),
          str([n for n, _ in first_events][-3:]))

    print("\n[3] 断流：客户端中途挂断 → 不扣额度")
    guest3, _ = new_guest(admin, "C")
    task3 = new_task(guest3, "C")
    req = urllib.request.Request(
        f"{BASE}/api/tasks/{task3}/chat",
        data=json.dumps({"text": '#fake {"chunks": 8, "delay_ms": 400}'}).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Forwarded-For", guest3.xff)
    resp = guest3.opener.open(req, timeout=60)
    head = resp.read(120)  # 只读开头就挂断
    resp.close()
    check("确实收到了开头", b"meta" in head or b"event" in head, head[:40].decode("utf-8", "replace"))
    time.sleep(3.5)
    used3 = quota_used(guest3)
    rows = db_rows(
        "SELECT status, COUNT(*) FROM ai_calls WHERE visitor_id = "
        "(SELECT id FROM visitors WHERE nickname = ?) GROUP BY status", ("m3C",)
    )
    statuses = dict(rows)
    check("断流不扣额度", used3 == 0, f"used={used3} 明细={statuses}")
    check("预扣已结算为失败（不留悬挂）", statuses.get("reserved", 0) == 0, str(statuses))

    print("\n[4] 额度用满 → 429")
    guest4, me4 = new_guest(admin, "D")
    vid = me4.get("id")
    rows = []
    now = datetime.now(TZ)
    for i in range(3):
        rows.append((
            f"seed{i}{vid}", vid, today(), "chat", "done",
            now.isoformat(timespec="seconds"),
            (now + timedelta(seconds=600)).isoformat(timespec="seconds"),
        ))
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.executemany(
        "INSERT INTO ai_calls(request_id, visitor_id, day, kind, status, created_at, expires_at) "
        "VALUES(?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    check("已塞满码额度 3 次", quota_used(guest4) == 3, f"used={quota_used(guest4)}")
    task4 = new_task(guest4, "D")
    status, body = guest4.call("POST", f"/api/tasks/{task4}/chat", {"text": '#fake {"chunks": 1}'})
    check("第 4 次回 429", status == 429, f"got {status} {body}")
    check("429 带 code=quota_exhausted", isinstance(body, dict) and body.get("code") == "quota_exhausted",
          str(body))
    check("429 文案说明额度", "额度" in json.dumps(body, ensure_ascii=False), str(body))

    print("\n[5] 上游报错 → 不扣额度")
    guest5, _ = new_guest(admin, "E")
    task5 = new_task(guest5, "E")
    status, events, _ = guest5.chat(task5, '#fake {"chunks": 6, "delay_ms": 20, "fail": "mid"}')
    names = [n for n, _ in events]
    check("流里出现 error 事件", "error" in names, str(names))
    err = next((p for n, p in events if n == "error"), {}) or {}
    check("错误信息可读", bool(err.get("message")), str(err)[:120])
    check("报错不扣额度", quota_used(guest5) == 0, f"used={quota_used(guest5)}")

    print("\n[6] 心跳：上游 9.5 秒没吐字")
    guest6, _ = new_guest(admin, "F")
    task6 = new_task(guest6, "F")
    status, events, raw = guest6.chat(task6, '#fake {"chunks": 1, "delay_ms": 9500}')
    check("流里有注释型心跳", ": ping" in raw, f"ping×{raw.count(': ping')}")
    check("慢响应仍能跑完", any(n == "done" for n, _ in events))
    check("慢响应照样扣一次", quota_used(guest6) == 1, f"used={quota_used(guest6)}")

    print("\n[7] 真实上游冒烟（不带 #fake）")
    guest7, _ = new_guest(admin, "G")
    task7 = new_task(guest7, "G")
    status, events, raw = guest7.chat(task7, "只回四个字：验收通过")
    names = [n for n, _ in events]
    text = "".join(p.get("t", "") for n, p in events if n == "text")
    usage = next((p for n, p in events if n == "usage"), {}) or {}
    check("真实调用 200", status == 200, f"got {status}")
    check("拿到正文", bool(text.strip()), text[:40])
    check("拿到真实 usage", (usage.get("in") or 0) > 0, str(usage))
    check("没有 error 事件", "error" not in names, str(names))

    total = len(PASS) + len(FAIL)
    print(f"\n{'=' * 46}\n通过 {len(PASS)}/{total}")
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print("  - " + name)
        return 1
    print("M3 流式与配额全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
