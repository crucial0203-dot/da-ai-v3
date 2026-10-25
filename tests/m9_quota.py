"""M9 验收：额度由邀请码决定（V3.1）。

跑法（服务需已在 8020 起好，建议 FAKE_UPSTREAM=1 DEBUG_MODE=1）：
    python tests/m9_quota.py

覆盖：
  1  旧码迁移：库里所有 invites.daily_quota 非空
  2  建码带日额 → 兑换 → /api/me 额度跟码
  3  管理端改码日额 → 访客额度即时跟随
  4  单人 quota_override 覆盖码额度；清空后回落
  5  访客列表带出 invite_quota；管理台改额度生效
  6  塞满码额度 → 429 带 code=quota_exhausted
  7  管理台之外：/api/meta 仍有兜底 daily_calls
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = os.environ.get("DA_BASE", "http://127.0.0.1:8020")
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "app.db"
TZ = timezone(timedelta(hours=8))

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'  OK ' if ok else '  !! '} {name}" + (f"  [{detail}]" if detail else ""))


def read_env() -> dict:
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = read_env()
ADMIN_PW = ENV.get("ADMIN_PASSWORD", "")


class Client:
    def __init__(self, xff: str = "10.10.0.1"):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar),
        )
        self.xff = xff

    def call(self, method: str, path: str, body=None, xff: str | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Forwarded-For", xff or self.xff)
        try:
            with self.opener.open(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw

    def has_cookie(self) -> bool:
        return any(c.name == "da_sid" for c in self.jar)


def db_rows(sql: str, params=()) -> list[tuple]:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def me_quota(client: Client) -> dict:
    _, me = client.call("GET", "/api/me")
    return me.get("quota") or {}


def main() -> int:
    if not DB_PATH.exists():
        print(f"找不到数据库 {DB_PATH}，服务没起来？")
        return 2
    admin = Client("10.10.0.10")
    status, _ = admin.call("POST", "/api/admin/login", {"password": ADMIN_PW})
    if status != 200:
        print(f"管理员登录失败 {status}，检查 .env 的 ADMIN_PASSWORD")
        return 2

    print("\n[1] 旧码迁移回填")
    rows = db_rows("SELECT COUNT(*) FROM invites WHERE daily_quota IS NULL")
    check("所有邀请码 daily_quota 均已回填", rows[0][0] == 0, f"NULL 行 {rows[0][0]}")
    fifty = db_rows("SELECT COUNT(*) FROM invites WHERE daily_quota = 50")
    check("存在日额 50 的码（回填或新建）", fifty[0][0] >= 1, f"count={fifty[0][0]}")

    print("\n[2] 建码带日额 → 兑换 → 额度跟码")
    status, body = admin.call("POST", "/api/admin/invites",
                              {"count": 1, "max_uses": 2, "days": 7, "daily_quota": 7,
                               "note": "m9"})
    code_a = (body.get("codes") or [""])[0]
    check("建码成功", status == 200 and bool(code_a), f"{status} {body}")
    guest = Client("10.10.0.11")
    status, _ = guest.call("POST", "/api/join", {"code": code_a, "nickname": "m9甲"})
    check("凭码进入", status == 200, f"got {status}")
    q = me_quota(guest)
    check("额度=码的 7 次", q.get("quota") == 7, str(q))

    print("\n[3] 改码日额 → 即时跟随")
    status, _ = admin.call("PATCH", f"/api/admin/invites/{code_a}", {"daily_quota": 9})
    check("PATCH 码日额 200", status == 200, f"got {status}")
    q = me_quota(guest)
    check("额度变为 9", q.get("quota") == 9, str(q))

    print("\n[4] 单人覆盖优先；清空回落")
    _, me = guest.call("GET", "/api/me")
    vid = me.get("id")
    status, _ = admin.call("PATCH", f"/api/admin/visitors/{vid}", {"quota_override": 2})
    check("设单人覆盖 200", status == 200, f"got {status}")
    q = me_quota(guest)
    check("覆盖后额度=2", q.get("quota") == 2, str(q))
    status, _ = admin.call("PATCH", f"/api/admin/visitors/{vid}", {"quota_override": 0})
    q = me_quota(guest)
    check("清空覆盖回落码额度 9", q.get("quota") == 9, str(q))

    print("\n[5] 访客列表带出 invite_quota")
    status, body = admin.call("GET", "/api/admin/visitors")
    row = next((v for v in body.get("visitors", []) if v["id"] == vid), None)
    check("列表 200 且能找到该访客", status == 200 and row is not None, f"{status}")
    check("invite_quota=9", row and row.get("invite_quota") == 9, str(row))

    print("\n[6] 塞满码额度 → 429 带 code")
    now = datetime.now(TZ)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    for i in range(9):
        conn.execute(
            "INSERT INTO ai_calls(request_id, visitor_id, day, kind, status, created_at, expires_at) "
            "VALUES(?,?,?,?, 'done', ?, ?)",
            (f"m9seed{i}{vid}", vid, today(), "chat",
             now.isoformat(timespec="seconds"),
             (now + timedelta(seconds=600)).isoformat(timespec="seconds")),
        )
    conn.commit()
    conn.close()
    q = me_quota(guest)
    check("已用 9 次", q.get("used") == 9, str(q))
    status, t = guest.call("POST", "/api/tasks", {"mode": "writing", "goal": "m9 配额验收"})
    task_id = (t.get("id") or t.get("task", {}).get("id") or "") if isinstance(t, dict) else ""
    status, body = guest.call("POST", f"/api/tasks/{task_id}/chat", {"text": '#fake {"chunks": 1}'})
    check("超额回 429", status == 429, f"got {status} {body}")
    check("429 code=quota_exhausted",
          isinstance(body, dict) and body.get("code") == "quota_exhausted", str(body))
    check("429 带 quota 数字", isinstance(body, dict) and body.get("quota") == 9, str(body))

    print("\n[7] meta 兜底键仍在")
    anon = Client("10.10.0.99")
    status, meta = anon.call("GET", "/api/meta")
    check("meta 200 且有 daily_calls / quota_fallback",
          status == 200 and "daily_calls" in meta and "quota_fallback" in meta,
          f"{status} keys={sorted(meta)[:8] if isinstance(meta, dict) else meta}")

    total = len(PASS) + len(FAIL)
    print(f"\n{'=' * 46}\n通过 {len(PASS)}/{total}")
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print("  - " + name)
        return 1
    print("M9 邀请码额度全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
