"""M11 验收：Skills 配置中心（访客端暴露 + 版本快照 + 内置 PPT 工作流）。

跑法（服务需已在 8020 起好，FAKE_UPSTREAM=1 DEBUG_MODE=1）：
    python tests/m11_skills.py

覆盖：
  1  管理员建 workflow 技能 → 草稿就位；FORBIDDEN 工具被拒
  2  访客越权 403；未发布的技能不出现在访客列表
  3  发布 → 1.0；访客列表/详情为只读 config（无 permissions/policy）
  4  版本不可原地改：改草稿不影响已发布内容；再发布 → 1.1
  5  回滚 1.0 → published_version 指回，访客看到 1.0 的内容
  6  内置 PPT 工作流已 seed，出现在访客 workflow 列表
  7  生成启动时 version 快照：params_json.skill 由服务端解析，客户端伪造的 skill 被剥离
  8  绑定未发布技能 → generate 400，且不扣额度
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import sqlite3
import sys
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

GOOD_META = {
    "name": "m11 测试流程",
    "description": "两节点测试工作流",
    "nodes": [
        {"key": "a", "label": "第一步", "deps": [], "tools": ["minimax.generate"], "retries": 1},
        {"key": "b", "label": "第二步", "deps": ["a"], "tools": ["artifact.write"], "retries": 0},
    ],
    "permissions": {"minimax.generate": True, "artifact.write": True},
    "params_schema": {},
    "policy": {"approval_after": "a", "max_retries_per_step": 2},
}


class Client:
    def __init__(self, xff: str = "10.12.0.1"):
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
            with self.opener.open(req, timeout=120) as resp:
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

    def sse(self, path: str, body: dict) -> tuple[int, list[str], str]:
        data = json.dumps(body).encode()
        req = urllib.request.Request(BASE + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "text/event-stream")
        req.add_header("X-Forwarded-For", self.xff)
        try:
            with self.opener.open(req, timeout=180) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            return exc.code, [], exc.read().decode("utf-8", "replace")[:300]
        events = []
        for block in raw.split("\n\n"):
            for line in block.split("\n"):
                if line.startswith("event:"):
                    events.append(line[6:].strip())
        return status, events, raw


def db_one(sql: str, params=()):
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def main() -> int:
    if not DB_PATH.exists():
        print(f"找不到数据库 {DB_PATH}，服务没起来？")
        return 2
    admin = Client("10.12.0.10")
    status, _ = admin.call("POST", "/api/admin/login", {"password": ADMIN_PW})
    if status != 200:
        print(f"管理员登录失败 {status}")
        return 2
    status, body = admin.call("POST", "/api/admin/invites",
                              {"count": 1, "max_uses": 1, "days": 7, "daily_quota": 20, "note": "m11"})
    code = (body.get("codes") or [""])[0]
    guest = Client("10.12.0.11")
    guest.call("POST", "/api/join", {"code": code, "nickname": "m11乙"})

    print("\n[1] 建技能与校验")
    status, body = admin.call("POST", "/api/admin/skills",
                              {"name": "m11 测试流程", "body": "# 测试\n第一步做 A，第二步做 B。", "meta": GOOD_META})
    check("建技能 200", status == 200, f"{status} {str(body)[:80]}")
    skill_id = (body.get("skill") or {}).get("id", "")
    check("技能进入草稿态", bool(skill_id) and (body.get("skill") or {}).get("has_draft") is True,
          str(body.get("skill"))[:80])

    bad_meta = dict(GOOD_META)
    bad_meta["permissions"] = {"shell.execute": True}
    status, body = admin.call("POST", "/api/admin/skills",
                              {"name": "越权技能", "body": "x", "meta": bad_meta})
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
    check("FORBIDDEN 工具被拒 400", status == 400 and detail.get("code") == "config_invalid",
          f"{status} {str(body)[:100]}")

    print("\n[2] 越权与未发布不可见")
    status, _ = guest.call("GET", "/api/admin/skills")
    check("访客访问管理端 403", status == 403, f"got {status}")
    status, body = guest.call("GET", "/api/skills?kind=workflow")
    ids = [s["id"] for s in body.get("skills", [])]
    check("未发布技能不在访客列表", skill_id not in ids, f"n={len(ids)}")

    print("\n[3] 发布 → 1.0，只读 config")
    status, body = admin.call("POST", f"/api/admin/skills/{skill_id}/publish")
    check("发布返回 1.0", status == 200 and body.get("version") == "1.0", str(body)[:80])
    status, body = guest.call("GET", "/api/skills?kind=workflow")
    entry = next((s for s in body.get("skills", []) if s["id"] == skill_id), None)
    check("发布后出现在访客列表", entry is not None and entry.get("version") == "1.0",
          str(entry)[:80] if entry else "缺失")
    check("访客视图不含 permissions/policy",
          entry is not None and "permissions" not in entry and "policy" not in entry, "")
    check("访客视图带节点标签", bool(entry) and [n["key"] for n in entry["nodes"]] == ["a", "b"],
          str(entry.get("nodes"))[:80] if entry else "")
    status, detail = guest.call("GET", f"/api/skills/{skill_id}")
    check("访客详情为只读 config", status == 200 and detail.get("body", "").startswith("# 测试")
          and "permissions" not in detail, str(detail)[:80])

    print("\n[4] 版本不可原地改；再发布 → 1.1")
    admin.call("PUT", f"/api/admin/skills/{skill_id}/draft",
               {"body": "# 测试 v1.1 草稿\n改了内容。", "meta": GOOD_META})
    status, body = admin.call("POST", f"/api/admin/skills/{skill_id}/publish")
    check("二次发布 1.1", status == 200 and body.get("version") == "1.1", str(body)[:80])
    status, body = admin.call("GET", f"/api/admin/skills/{skill_id}")
    pub = body.get("published") or {}
    check("发布版本不可原地改（1.0 转 archived）",
          pub.get("version") == "1.1" and pub.get("body", "").startswith("# 测试 v1.1"),
          str(pub.get("version")))

    print("\n[5] 回滚 1.0")
    status, body = admin.call("POST", f"/api/admin/skills/{skill_id}/rollback", {"version": "1.0"})
    check("回滚 200", status == 200 and body.get("version") == "1.0", str(body)[:80])
    status, body = guest.call("GET", f"/api/skills/{skill_id}")
    check("回滚后访客看到 1.0 内容", body.get("version") == "1.0" and body.get("body", "").startswith("# 测试\n"),
          f"v={body.get('version')}")

    print("\n[6] 内置 PPT 工作流")
    row = db_one("SELECT id, published_version, enabled FROM skills WHERE kind='workflow' AND is_builtin=1")
    check("内置 PPT 技能已 seed 且已发布",
          row is not None and row["published_version"] == "1.0" and row["enabled"] == 1,
          str(dict(row) if row else None)[:100])
    builtin_id = row["id"] if row else ""
    status, body = guest.call("GET", "/api/skills?kind=workflow")
    entry = next((s for s in body.get("skills", []) if s["id"] == builtin_id), None)
    keys = [n["key"] for n in (entry or {}).get("nodes", [])]
    check("内置技能在访客列表且六节点",
          entry is not None and keys == ["plan", "research", "content", "render", "validate", "deliver"],
          str(keys))

    print("\n[7] version 快照")
    status, t = guest.call("POST", "/api/tasks", {
        "mode": "writing", "goal": "m11 快照验收",
        "params": {"skill_id": builtin_id, "skill": {"id": "fake", "version": "9.9"}},
    })
    task_id = t.get("id") or t.get("task", {}).get("id")
    check("建任务（带 skill_id + 伪造 skill）", bool(task_id), str(t)[:80])
    conn = sqlite3.connect(str(DB_PATH))
    stored_pre = json.loads(conn.execute(
        "SELECT params_json FROM tasks WHERE id = ?", (task_id,)).fetchone()[0])
    conn.close()
    check("建任务时伪造 skill 已剥离", "skill" not in stored_pre, str(stored_pre)[:100])
    status, events, raw = guest.sse(f"/api/tasks/{task_id}/generate",
                                    {"doc_type": "docx", "word_count": 300})
    check("生成流 200", status == 200, f"got {status} {raw[:120]}")
    snap = db_one("SELECT params_json FROM tasks WHERE id = ?", (task_id,))
    params = json.loads(snap["params_json"]) if snap else {}
    skill_snap = params.get("skill") or {}
    check("快照由服务端解析", skill_snap.get("id") == builtin_id and skill_snap.get("version") == "1.0"
          and skill_snap.get("name", "").startswith("PPT"),
          str(skill_snap)[:100])

    print("\n[8] 绑定未发布技能 → 400 不扣额度")
    status, body = guest.call("GET", "/api/me")
    used_before = (body.get("quota") or {}).get("used")
    status, t = guest.call("POST", "/api/tasks", {
        "mode": "writing", "goal": "m11 未发布绑定", "params": {"skill_id": skill_id},
    })
    # skill_id 现在指向已回滚但已发布的 1.0 —— 先造一个真正未发布的：再建一个不发
    status, body = admin.call("POST", "/api/admin/skills",
                              {"name": "m11 未发布", "body": "草稿而已", "meta": GOOD_META})
    unpub_id = (body.get("skill") or {}).get("id", "")
    status, t = guest.call("POST", "/api/tasks",
                           {"mode": "writing", "goal": "m11 未发布绑定", "params": {"skill_id": unpub_id}})
    task_id2 = t.get("id") or t.get("task", {}).get("id")
    status, events, raw = guest.sse(f"/api/tasks/{task_id2}/generate", {"doc_type": "docx"})
    check("未发布绑定 generate 400", status == 400, f"got {status} {raw[:120]}")
    status, body = guest.call("GET", "/api/me")
    used_after = (body.get("quota") or {}).get("used")
    check("失败请求不扣额度", used_after == used_before, f"{used_before} -> {used_after}")

    total = len(PASS) + len(FAIL)
    print(f"\n{'=' * 46}\n通过 {len(PASS)}/{total}")
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print("  - " + name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
