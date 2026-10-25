"""M2 验收：邀请码闭环。

跑法（服务需已在 8020 起好）：
    python tests/m2_invite.py

覆盖：
  1  管理员密码错 → 401；对 → 200
  2  建码 → 兑换 → /api/me → 退出 → /api/me 401
  3  次数用完 → 400（并发下不超发）
  4  过期 → 400
  5  停用 → 400
  6  访客会话打管理接口 → 403；无会话 → 401
  7  同 IP 连打超过 12 次 → 429
  8  管理台删访客，连带清掉会话与调用记录
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("DA_BASE", "http://127.0.0.1:8020")
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "app.db"

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
    """一个自带 cookie 罐的客户端。xff 用于隔离限流桶。"""

    def __init__(self, xff: str = "10.9.0.1"):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),  # 本机设了 HTTP_PROXY，不关掉会把 127.0.0.1 也代理走
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


def db_exec(sql: str, params=()) -> None:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    if not DB_PATH.exists():
        print(f"找不到数据库 {DB_PATH}，服务没起来？")
        return 2
    admin = Client("10.9.0.10")

    print("\n[1] 管理员登录")
    status, _ = admin.call("POST", "/api/admin/login", {"password": "definitely-wrong"})
    check("密码错回 401", status == 401, f"got {status}")
    status, _ = admin.call("POST", "/api/admin/login", {"password": ADMIN_PW})
    check("密码对回 200 并下 cookie", status == 200 and admin.has_cookie(), f"got {status}")

    print("\n[2] 建码 → 兑换 → me → 退出")
    status, body = admin.call("POST", "/api/admin/invites",
                              {"count": 1, "max_uses": 2, "days": 7, "daily_quota": 50})
    code_a = (body.get("codes") or [""])[0] if status == 200 else ""
    check("管理员建码成功", status == 200 and len(code_a) == 8, f"got {status} {code_a}")

    guest = Client("10.9.0.11")
    status, body = guest.call("POST", "/api/join", {"code": code_a, "nickname": "测试甲"})
    check("凭码进入回 200", status == 200, f"got {status} {body}")
    check("进入后拿到会话 cookie", guest.has_cookie())

    status, me = guest.call("GET", "/api/me")
    ok_me = (
        status == 200
        and me.get("nickname") == "测试甲"
        and me.get("role") == "visitor"
        and (me.get("quota") or {}).get("quota") == 50
    )
    check("/api/me 身份与额度跟码 50 正确", ok_me, f"got {status} {me}")

    status, body = guest.call("POST", "/api/join", {"code": code_a, "nickname": "测试乙"},
                              xff="10.9.0.12")
    check("同码第二次仍可进（max_uses=2）", status == 200, f"got {status} {body}")
    status, body = guest.call("POST", "/api/join", {"code": code_a, "nickname": "测试丙"},
                              xff="10.9.0.13")
    check("第三次被拒（次数用完）", status == 400 and "用完" in str(body), f"got {status} {body}")

    status, _ = guest.call("POST", "/api/auth/logout")
    check("退出回 200", status == 200, f"got {status}")
    status, _ = guest.call("GET", "/api/me")
    check("退出后 /api/me 回 401", status == 401, f"got {status}")

    print("\n[3] 不存在的码")
    fresh = Client("10.9.0.14")
    status, body = fresh.call("POST", "/api/join", {"code": "ZZZZZZZZ", "nickname": "测试丁"})
    check("不存在回 400", status == 400 and "不存在" in str(body), f"got {status} {body}")

    print("\n[4] 过期码")
    status, body = admin.call("POST", "/api/admin/invites", {"count": 1, "max_uses": 5, "days": 1})
    code_exp = (body.get("codes") or [""])[0]
    db_exec("UPDATE invites SET expires_at = ? WHERE code = ?",
            ("2020-01-01T00:00:00+08:00", code_exp))
    status, body = fresh.call("POST", "/api/join", {"code": code_exp, "nickname": "测试戊"},
                              xff="10.9.0.15")
    check("过期码回 400", status == 400 and "过期" in str(body), f"got {status} {body}")

    print("\n[5] 停用码")
    status, body = admin.call("POST", "/api/admin/invites", {"count": 1, "max_uses": 5, "days": 7})
    code_off = (body.get("codes") or [""])[0]
    status, _ = admin.call("PATCH", f"/api/admin/invites/{code_off}", {"disabled": True})
    check("停用接口回 200", status == 200, f"got {status}")
    status, body = fresh.call("POST", "/api/join", {"code": code_off, "nickname": "测试己"},
                              xff="10.9.0.16")
    check("停用码回 400", status == 400 and "停用" in str(body), f"got {status} {body}")

    print("\n[6] 越权")
    joined = Client("10.9.0.20")
    status, body = admin.call("POST", "/api/admin/invites", {"count": 1, "max_uses": 5, "days": 7})
    code_c = (body.get("codes") or [""])[0]
    joined.call("POST", "/api/join", {"code": code_c, "nickname": "测试庚"})
    status, _ = joined.call("GET", "/api/admin/overview")
    check("访客打管理接口回 403", status == 403, f"got {status}")
    status, _ = joined.call("GET", "/api/admin/visitors")
    check("访客列访客列表回 403", status == 403, f"got {status}")
    anon = Client("10.9.0.21")
    status, _ = anon.call("GET", "/api/admin/overview")
    check("无会话打管理接口回 401", status == 401, f"got {status}")

    print("\n[7] 并发只放满额")
    status, body = admin.call("POST", "/api/admin/invites", {"count": 1, "max_uses": 3, "days": 7})
    code_race = (body.get("codes") or [""])[0]
    results: list[int] = []
    lock = threading.Lock()

    def racer(i: int) -> None:
        c = Client(f"10.9.1.{i}")
        st, _ = c.call("POST", "/api/join", {"code": code_race, "nickname": f"并发{i:02d}"})
        with lock:
            results.append(st)

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok_n = results.count(200)
    check("8 并发抢 3 个名额，恰好 3 个成功", ok_n == 3, f"成功 {ok_n}，分布 {sorted(results)}")

    print("\n[8] 限流：同 IP 连打")
    ip_fixed = "10.9.2.1"
    flood = Client(ip_fixed)
    codes_status = []
    for _ in range(13):
        st, _ = flood.call("POST", "/api/join", {"code": "ZZZZZZZZ", "nickname": "限流测试"},
                           xff=ip_fixed)
        codes_status.append(st)
    check("第 13 次被限流为 429", codes_status[-1] == 429, f"分布 {codes_status}")

    print("\n[9] 删访客连带清理")
    status, listing = admin.call("GET", "/api/admin/visitors")
    target = next((v for v in listing.get("visitors", []) if v["nickname"] == "测试庚"), None)
    check("能查到刚建的访客", target is not None)
    if target:
        status, _ = admin.call("DELETE", f"/api/admin/visitors/{target['id']}")
        check("删除回 200", status == 200, f"got {status}")
        conn = sqlite3.connect(str(DB_PATH))
        left = conn.execute("SELECT COUNT(*) FROM sessions WHERE subject_id = ?",
                            (target["id"],)).fetchone()[0]
        gone = conn.execute("SELECT COUNT(*) FROM visitors WHERE id = ?",
                            (target["id"],)).fetchone()[0]
        conn.close()
        check("会话一并清掉", left == 0, f"剩 {left}")
        check("访客记录已删", gone == 0, f"剩 {gone}")
        status, _ = joined.call("GET", "/api/me")
        check("被删访客的旧会话失效（401）", status == 401, f"got {status}")

    print("\n[10] 审计有记录")
    status, body = admin.call("GET", "/api/admin/audit?limit=50")
    actions = {item["action"] for item in body.get("items", [])} if status == 200 else set()
    check("审计含 invite.create", "invite.create" in actions, f"got {sorted(actions)}")
    check("审计含 visitor.join", "visitor.join" in actions)

    total = len(PASS) + len(FAIL)
    print(f"\n{'=' * 46}\n通过 {len(PASS)}/{total}")
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print("  - " + name)
        return 1
    print("M2 邀请码闭环全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
