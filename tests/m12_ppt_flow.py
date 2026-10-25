"""M12 验收：PPT 六节点工作流（渲染前确认 + 续跑 + 修改 + 作废 + 校验重试）。

跑法（服务需已在 8020 起好，FAKE_UPSTREAM=1 DEBUG_MODE=1）：
    python tests/m12_ppt_flow.py

覆盖：
  1  工作流路由：pptx 生成建出六节点（plan..deliver），不走 intake/outline
  2  content 完成后 waiting_approval；大纲可从任务详情重建；未确认前无交付物
  3  approve 续跑 render→validate→deliver，纯本地渲染不扣次；交付 .pptx
  4  非等待态 approve/revise/cancel 一律 409
  5  revise 重跑 content（扣 1 次），下游节点作废回 todo，再次等待确认
  6  cancel 回 active
  7  validate 失败 → 按口径自动重试内容（补计 1 次）；重试仍失败则 validate failed
  8  validate_slides 两级校验（纯函数，直接 import）
"""
from __future__ import annotations

import copy
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

GOOD = {
    "title": "行政协议诉讼要点",
    "subtitle": "读书会汇报",
    "chapters": [
        {"heading": "概念与边界", "question": "什么是行政协议"},
        {"heading": "裁判规则", "question": "法院怎么审"},
    ],
    "slides": [
        {"layout": "cover", "title": "行政协议诉讼要点", "subtitle": "读书会汇报",
         "bullets": [], "notes": "开场介绍主题与汇报人，说明材料来源。"},
        {"layout": "bullets", "title": "概念与边界",
         "bullets": ["行政协议的定义与要素", "与民事合同的区分标准"],
         "notes": "先讲定义，再讲区分标准，最后给两个例子。"},
        {"layout": "two_col", "title": "裁判规则",
         "bullets": ["合法性审查优先", "补偿与赔偿的界分"],
         "notes": "结合案例说明法院的审查顺序与裁判思路。"},
        {"layout": "end", "title": "谢谢", "bullets": [],
         "notes": "收尾并进入讨论环节。"},
    ],
}

LONG = copy.deepcopy(GOOD)
LONG["slides"][1]["bullets"] = ["这页要点写得太长导致单页字数超标，" + "长" * 260]


def fake_goal(structure: dict) -> str:
    return "#fake " + json.dumps({"text": json.dumps(structure, ensure_ascii=False)}, ensure_ascii=False)


class Client:
    def __init__(self, xff: str = "10.13.0.1"):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar),
        )
        self.xff = xff

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Forwarded-For", self.xff)
        try:
            with self.opener.open(req, timeout=180) as resp:
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

    def sse(self, path: str, body: dict) -> tuple[int, list[tuple[str, dict]]]:
        data = json.dumps(body).encode()
        req = urllib.request.Request(BASE + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "text/event-stream")
        req.add_header("X-Forwarded-For", self.xff)
        try:
            with self.opener.open(req, timeout=240) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            return exc.code, []
        events = []
        for block in raw.split("\n\n"):
            name, lines = "message", []
            for line in block.split("\n"):
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    lines.append(line[5:].strip())
            if lines:
                try:
                    events.append((name, json.loads("\n".join(lines))))
                except json.JSONDecodeError:
                    events.append((name, {"raw": "\n".join(lines)}))
        return status, events


def db_q(sql: str, params=()):
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def used_of(client: Client) -> int:
    _, me = client.call("GET", "/api/me")
    return (me.get("quota") or {}).get("used") or 0


def main() -> int:
    if not DB_PATH.exists():
        print(f"找不到数据库 {DB_PATH}，服务没起来？")
        return 2
    admin = Client("10.13.0.10")
    status, _ = admin.call("POST", "/api/admin/login", {"password": ADMIN_PW})
    if status != 200:
        print(f"管理员登录失败 {status}")
        return 2
    status, body = admin.call("POST", "/api/admin/invites",
                              {"count": 1, "max_uses": 1, "days": 7, "daily_quota": 20, "note": "m12"})
    code = (body.get("codes") or [""])[0]
    guest = Client("10.13.0.11")
    guest.call("POST", "/api/join", {"code": code, "nickname": "m12丙"})

    status, body = guest.call("GET", "/api/skills?kind=workflow")
    builtin = next((s for s in body.get("skills", []) if s.get("is_builtin")), None)
    if not builtin:
        print("找不到内置 PPT 工作流技能")
        return 2

    print("\n[1] 工作流路由与 waiting_approval")
    status, t = guest.call("POST", "/api/tasks", {
        "mode": "slides", "goal": fake_goal(GOOD), "params": {"skill_id": builtin["id"]},
    })
    task_a = t.get("id") or t.get("task", {}).get("id")
    status, events = guest.sse(f"/api/tasks/{task_a}/generate", {"doc_type": "pptx", "page_count": 4})
    names = [n for n, _ in events]
    check("生成流 200", status == 200, f"got {status}")
    node_keys = [d.get("key") for n, d in events if n == "node"]
    check("六节点事件", all(k in node_keys for k in ["plan", "research", "content"]),
          str(node_keys))
    check("不走旧 intake/outline 节点", "intake" not in node_keys and "outline" not in node_keys,
          str(node_keys))
    check("以 approval 事件收尾", "approval" in names, str(names[-4:]))
    status, detail = guest.call("GET", f"/api/tasks/{task_a}")
    check("任务进入 waiting_approval", detail.get("task", {}).get("status") == "waiting_approval",
          str(detail.get("task", {}).get("status")))
    approval = detail.get("approval") or {}
    check("详情可重建大纲", approval.get("title") == GOOD["title"] and len(approval.get("slides") or []) == 4,
          str(approval)[:100])
    keys = [n["key"] for n in detail.get("nodes", [])]
    check("节点为六键工作流",
          keys == ["plan", "research", "content", "render", "validate", "deliver"], str(keys))
    n_files = len([f for f in detail.get("files", []) if f["kind"] == "deliverable"])
    check("未确认前无交付物", n_files == 0, f"n={n_files}")

    print("\n[2] approve 续跑（不扣次）")
    used_before = used_of(guest)
    status, events = guest.sse(f"/api/tasks/{task_a}/approve", {})
    names = [n for n, _ in events]
    check("approve 200", status == 200, f"got {status}")
    check("render/validate/deliver 事件齐",
          all(any(n == "node" and d.get("key") == k and d.get("status") == "done" for n, d in events)
              for k in ["render", "validate", "deliver"]), str(names[-8:]))
    result = next((d for n, d in events if n == "result"), {})
    check("result ok 且有 file_id", result.get("ok") is True and str(result.get("file_id", "")).startswith("f_"),
          str(result)[:100])
    used_after = used_of(guest)
    check("approve 不扣额度", used_after == used_before, f"{used_before} -> {used_after}")
    status, detail = guest.call("GET", f"/api/tasks/{task_a}")
    check("任务 delivered", detail.get("task", {}).get("status") == "delivered",
          str(detail.get("task", {}).get("status")))
    deliv = [f for f in detail.get("files", []) if f["kind"] == "deliverable"]
    check("交付 .pptx", len(deliv) == 1 and deliv[0]["name"].endswith(".pptx"), str(deliv)[:100])
    sys_msgs = [m for m in detail.get("messages", []) if m.get("role") == "system"]
    check("交付卡片落消息", len(sys_msgs) >= 1, f"n={len(sys_msgs)}")

    print("\n[3] 非等待态一律 409")
    for action in ["approve", "revise", "cancel"]:
        status, _ = guest.call("POST", f"/api/tasks/{task_a}/{action}", {"feedback": "x"} if action == "revise" else {})
        check(f"{action} 409", status == 409, f"got {status}")

    print("\n[4] revise：重跑 content，下游作废")
    status, t = guest.call("POST", "/api/tasks", {
        "mode": "slides", "goal": fake_goal(GOOD), "params": {"skill_id": builtin["id"]},
    })
    task_b = t.get("id") or t.get("task", {}).get("id")
    guest.sse(f"/api/tasks/{task_b}/generate", {"doc_type": "pptx", "page_count": 4})
    used_before = used_of(guest)
    status, events = guest.sse(f"/api/tasks/{task_b}/revise", {"feedback": "第 2 页换成数据对比"})
    check("revise 200", status == 200, f"got {status}")
    result = next((d for n, d in events if n == "result"), {})
    check("revise 后再次等待确认", result.get("ok") is True and result.get("waiting_approval") is True,
          str(result)[:100])
    used_after = used_of(guest)
    check("revise 扣 1 次", used_after == used_before + 1, f"{used_before} -> {used_after}")
    status, detail = guest.call("GET", f"/api/tasks/{task_b}")
    check("任务 waiting_approval", detail.get("task", {}).get("status") == "waiting_approval",
          str(detail.get("task", {}).get("status")))
    stat = {n["key"]: n["status"] for n in detail.get("nodes", [])}
    check("content done", stat.get("content") == "done", str(stat))
    check("下游作废回 todo", all(stat.get(k) == "todo" for k in ["render", "validate", "deliver"]), str(stat))
    approval = detail.get("approval") or {}
    check("修改后大纲仍在", approval.get("title") == GOOD["title"], str(approval)[:80])

    print("\n[5] cancel")
    status, t = guest.call("POST", "/api/tasks", {
        "mode": "slides", "goal": fake_goal(GOOD), "params": {"skill_id": builtin["id"]},
    })
    task_c = t.get("id") or t.get("task", {}).get("id")
    guest.sse(f"/api/tasks/{task_c}/generate", {"doc_type": "pptx", "page_count": 4})
    status, body = guest.call("POST", f"/api/tasks/{task_c}/cancel")
    check("cancel 200", status == 200, f"{status} {str(body)[:60]}")
    status, detail = guest.call("GET", f"/api/tasks/{task_c}")
    check("cancel 回 active", detail.get("task", {}).get("status") == "active",
          str(detail.get("task", {}).get("status")))

    print("\n[6] validate 失败 → 内容重试（补计 1 次）仍失败 → validate failed")
    status, t = guest.call("POST", "/api/tasks", {
        "mode": "slides", "goal": fake_goal(LONG), "params": {"skill_id": builtin["id"]},
    })
    task_d = t.get("id") or t.get("task", {}).get("id")
    status, events = guest.sse(f"/api/tasks/{task_d}/generate", {"doc_type": "pptx", "page_count": 4})
    check("超长页也能过两级校验进等待", any(n == "approval" for n, _ in events), "")
    used_before = used_of(guest)
    status, events = guest.sse(f"/api/tasks/{task_d}/approve", {})
    result = next((d for n, d in events if n == "result"), {})
    check("approve 报告校验失败", result.get("ok") is False and "report" in result, str(result)[:120])
    stat = {n["node_key"]: n["status"] for n in db_q(
        "SELECT node_key, status FROM task_nodes WHERE task_id = ?", (task_d,))}
    check("validate failed", stat.get("validate") == "failed", str(stat))
    check("重试后 content 仍 done", stat.get("content") == "done", str(stat))
    status, detail = guest.call("GET", f"/api/tasks/{task_d}")
    n_deliv = len([f for f in detail.get("files", []) if f["kind"] == "deliverable"])
    check("校验不过不交付", n_deliv == 0, f"n={n_deliv}")
    check("任务停在 failed", detail.get("task", {}).get("status") == "failed",
          str(detail.get("task", {}).get("status")))
    used_after = used_of(guest)
    check("内容重试补计 1 次", used_after == used_before + 1, f"{used_before} -> {used_after}")

    print("\n[7] validate_slides 两级校验（纯函数）")
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DATA_DIR", str(ROOT / "data"))
    try:
        from app.services.workflow import ppt as pf
        errs = pf.validate_slides({})
        check("空对象报结构错", any("title" in e for e in errs) and any("slides" in e for e in errs), str(errs))
        bad = {"title": "x", "slides": [{"layout": "bullets", "title": "页"}]}
        errs = pf.validate_slides(bad)
        check("缺 notes/bullets 被拦", any("讲稿" in e for e in errs), str(errs))
        errs = pf.validate_slides(GOOD, 4)
        check("合法结构零错误", errs == [], str(errs))
        errs = pf.validate_slides(GOOD, 10)
        check("页数偏差被拦", any("页数" in e for e in errs), str(errs))
    except Exception as exc:
        check("validate_slides 可导入", False, f"{type(exc).__name__}: {exc}")

    total = len(PASS) + len(FAIL)
    print(f"\n{'=' * 46}\n通过 {len(PASS)}/{total}")
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print("  - " + name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
