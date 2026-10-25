"""M25 验收：P1-4 停止可继续（API 层）。

跑法（需 8020 已起，且带 FAKE_UPSTREAM=1）：
    python tests/m25_stop_resume.py

覆盖：
  S1 流式中途掐断 → 落一条 status='stopped'、stopped=1、content 是半截正文
     （含思考不含正文的掐断不算——见 S5）
  S2 GET 任务只加字段：stopped/parent_id/branch_index 都在，老字段不变
  S3 继续写 → 直播第一段就是半截正文；新消息 content = 半截 + 续跑文本；
     旧 stopped 消息让位 aborted（不再进上下文）；分支槽位与旧消息对齐
  S4 丢弃 → 旧消息 aborted、不产生新消息、不调模型
  S5 一个字都没拿到就掐断 → 仍是 aborted（stopped 只留给有半截正文的）
  S6 没有 stopped 消息时继续 → 404 且不动数据
  S7 并发保护：另一条流还在跑时点继续 → 409，旧半截原封不动

掐断用裸 http.client：读到标志就 close()  sockets，让服务端看到客户端断开
（uvicorn 收 EOF → http.disconnect → 取消流式任务 → 走 abort 分支落库）。
"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import BASE, Client, admin_login, check, join, make_invites, stream, summary  # noqa: E402

_parsed = urllib.parse.urlparse(BASE)
HOST = _parsed.hostname or "127.0.0.1"
PORT = _parsed.port or 80


def cookie_of(client: Client) -> str:
    for c in client.jar:
        if c.name == "da_sid":
            return f"{c.name}={c.value}"
    return ""


def stream_then_abort(client: Client, path: str, body: dict, marker: str = "第2段",
                      budget: int = 800, deadline_s: float = 12.0):
    """发 SSE 请求，读到 marker（或读满 budget / 超时）就掐断连接。

    返回 (status, raw)。掐断是故意的：半截正文能不能落成 stopped 全看这一下。
    """
    conn = http.client.HTTPConnection(HOST, PORT, timeout=30)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    cookie = cookie_of(client)
    if cookie:
        headers["Cookie"] = cookie
    conn.request("POST", path, body=payload, headers=headers)
    resp = conn.getresponse()
    if resp.status != 200:
        text = resp.read().decode("utf-8", "replace")
        conn.close()
        return resp.status, text
    buf = ""
    deadline = time.time() + deadline_s
    while marker not in buf and len(buf) < budget and time.time() < deadline:
        piece = resp.read1(512)
        if not piece:
            break
        buf += piece.decode("utf-8", "replace")
    conn.close()
    return resp.status, buf


def assistant_msgs(client: Client, task_id: str) -> list[dict]:
    _, d = client.get(f"/api/tasks/{task_id}")
    return [m for m in (d.get("messages") or []) if m.get("role") == "assistant"]


def wait_status(client: Client, task_id: str, status: str, timeout: float = 15.0):
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = assistant_msgs(client, task_id)
        hit = [m for m in last if m.get("status") == status]
        if hit:
            return hit[-1], last
        time.sleep(0.3)
    return None, last


def task_id_of(data: dict) -> str:
    return data.get("id") or data.get("task", {}).get("id") or ""


def main() -> int:
    password = ""
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("ADMIN_PASSWORD="):
            password = line.split("=", 1)[1].strip()
    admin = admin_login(password)
    codes = make_invites(admin, count=4, quota=40, note="m25")
    clients = []
    for i, code in enumerate(codes):
        c = Client()
        join(c, code, f"m25停止{i}")
        clients.append(c)

    # 6 段 × 400ms：够读到第 2 段再掐断，也保证流没结束
    SLOW = {"chunks": 6, "delay_ms": 400}
    slow_text = f"#fake {json.dumps(SLOW, ensure_ascii=False)}"

    # ---- S1/S2/S3：掐断 → stopped → 继续写 ----
    c0 = clients[0]
    _, task = c0.post("/api/tasks", {"mode": "research", "goal": "m25 停止继续"})
    tid = task_id_of(task)
    status, raw = stream_then_abort(c0, f"/api/tasks/{tid}/chat", {"text": slow_text, "web_search": False})
    check("S1 流式请求 200", status == 200, str(status))
    check("S1 掐断前读到了正文", "第2段" in raw, raw[-160:])
    stopped, msgs = wait_status(c0, tid, "stopped")
    check("S1 落成 status=stopped", stopped is not None,
          str([(m["id"], m["status"], len(m["content"])) for m in msgs]))
    if stopped:
        check("S1 stopped 行 stopped=True", stopped.get("stopped") is True, str(stopped.get("stopped")))
        check("S1 content 是半截（有第1段、无第6段）",
              "第1段" in stopped["content"] and "第6段" not in stopped["content"],
              stopped["content"])
        check("S2 GET 带 parent_id 字段", "parent_id" in stopped, str(sorted(stopped.keys())))
        check("S2 GET 带 branch_index 字段", "branch_index" in stopped)
        check("S2 首版 parent_id 为空", stopped.get("parent_id") is None, str(stopped.get("parent_id")))
        check("S2 首版 branch_index 为空", stopped.get("branch_index") is None, str(stopped.get("branch_index")))
        prefix = stopped["content"]
        stopped_id = stopped["id"]

        # S3 继续写：SSE 第一段 text 就是半截正文，随后是续跑文本
        events = stream(c0, f"/api/tasks/{tid}/chat/continue", {})
        texts = [str(d.get("t") or "") for e, d in events if e == "text"]
        full = "".join(texts)
        check("S3 直播第一段就是半截正文", full.startswith(prefix), full[:80])
        dones = [d for e, d in events if e == "done"]
        check("S3 有 done 事件", bool(dones), str(events[-2:])[:200])
        if dones:
            check("S3 done status=done", dones[0].get("status") == "done", str(dones[0])[:160])

        msgs = assistant_msgs(c0, tid)
        done_new = [m for m in msgs if m.get("status") == "done"]
        check("S3 恰好一条 done", len(done_new) == 1, str([(m["id"], m["status"]) for m in msgs]))
        if done_new:
            new = done_new[0]
            check("S3 新 content = 半截 + 续跑（更长且以半截开头）",
                  new["content"].startswith(prefix) and len(new["content"]) > len(prefix),
                  f"{len(prefix)} -> {len(new['content'])}")
            check("S3 续跑写完了全部 6 段", "第6段" in new["content"], new["content"][-60:])
            check("S3 新消息 parent_id 指向旧半截", new.get("parent_id") == stopped_id,
                  f"{new.get('parent_id')} vs {stopped_id}")
            check("S3 新消息 branch_index=0（接班人占旧槽位）", new.get("branch_index") == 0,
                  str(new.get("branch_index")))
        old = [m for m in msgs if m["id"] == stopped_id]
        if old:
            check("S3 旧 stopped 消息让位为 aborted", old[0]["status"] == "aborted",
                  old[0]["status"])
            check("S3 旧消息 stopped 标记已清", not old[0].get("stopped"), str(old[0].get("stopped")))
        # 让位后上下文里不该再有它：再续一次应 404（没有 stopped 了）
        status2, _ = c0.post(f"/api/tasks/{tid}/chat/continue", {})
        check("S3b 让位后再继续 → 404", status2 == 404, str(status2))

    # ---- S4/S6：丢弃 ----
    c1 = clients[1]
    _, task2 = c1.post("/api/tasks", {"mode": "research", "goal": "m25 丢弃"})
    tid2 = task_id_of(task2)
    stream_then_abort(c1, f"/api/tasks/{tid2}/chat", {"text": slow_text, "web_search": False})
    stopped2, _ = wait_status(c1, tid2, "stopped")
    check("S4 第二条也落成 stopped", stopped2 is not None)
    if stopped2:
        before = assistant_msgs(c1, tid2)
        status, r = c1.post(f"/api/tasks/{tid2}/chat/continue", {"discard": True})
        check("S4 丢弃返回 ok+discarded", status == 200 and r.get("discarded") is True, str(r)[:160])
        after = assistant_msgs(c1, tid2)
        check("S4 丢弃不产生新消息", len(after) == len(before), f"{len(before)} -> {len(after)}")
        old2 = [m for m in after if m["id"] == stopped2["id"]]
        if old2:
            check("S4 丢弃后旧消息 aborted", old2[0]["status"] == "aborted", old2[0]["status"])
            check("S4 丢弃后 stopped 标记已清", not old2[0].get("stopped"))
        # S6：没有 stopped 时继续 → 404 且不动数据
        snap = assistant_msgs(c1, tid2)
        status, r = c1.post(f"/api/tasks/{tid2}/chat/continue", {})
        check("S6 没有 stopped 时继续 → 404", status == 404, str(status))
        snap2 = assistant_msgs(c1, tid2)
        check("S6 404 不动任何数据", snap == snap2)

    # ---- S5：空内容掐断 → 仍是 aborted ----
    c2 = clients[2]
    _, task3 = c2.post("/api/tasks", {"mode": "research", "goal": "m25 空停止"})
    tid3 = task_id_of(task3)
    # 1500ms 才吐第一个字：读到 meta 就掐，一个字都拿不到
    status, raw = stream_then_abort(
        c2, f"/api/tasks/{tid3}/chat",
        {"text": '#fake {"chunks": 6, "delay_ms": 1500}', "web_search": False},
        marker="event: meta", budget=600,
    )
    check("S5 请求 200", status == 200, str(status))
    _, msgs3 = wait_status(c2, tid3, "stopped", timeout=4.0)
    msgs3 = assistant_msgs(c2, tid3)
    check("S5 没有 stopped 行", not any(m["status"] == "stopped" for m in msgs3),
          str([(m["id"], m["status"]) for m in msgs3]))
    check("S5 落成 aborted", bool(msgs3) and all(m["status"] == "aborted" for m in msgs3),
          str([(m["id"], m["status"]) for m in msgs3]))
    check("S5 aborted 行 content 为空", all(not m["content"] for m in msgs3),
          str([len(m["content"]) for m in msgs3]))

    # ---- S7：并发保护 ----
    c3 = clients[3]
    _, task4 = c3.post("/api/tasks", {"mode": "research", "goal": "m25 并发守卫"})
    tid4 = task_id_of(task4)
    stream_then_abort(c3, f"/api/tasks/{tid4}/chat", {"text": slow_text, "web_search": False})
    stopped4, _ = wait_status(c3, tid4, "stopped")
    check("S7 备好一条 stopped", stopped4 is not None)
    _, task5 = c3.post("/api/tasks", {"mode": "research", "goal": "m25 并发占位"})
    tid5 = task_id_of(task5)

    holder: dict = {}

    def run_blocker() -> None:
        # 占一条慢流：marker 永不可能出现，读到底（deadline 兜底）
        holder["status"], holder["raw"] = stream_then_abort(
            c3, f"/api/tasks/{tid5}/chat",
            {"text": '#fake {"chunks": 6, "delay_ms": 500}', "web_search": False},
            marker="__never__", budget=10 ** 6, deadline_s=20.0,
        )

    t = threading.Thread(target=run_blocker)
    t.start()
    time.sleep(2.5)  # 等占位流真的 reserve 上
    status, r = c3.post(f"/api/tasks/{tid4}/chat/continue", {})
    check("S7 有流在跑时继续 → 409", status == 409, f"{status} {str(r)[:120]}")
    t.join(40)
    msgs4 = assistant_msgs(c3, tid4)
    still = [m for m in msgs4 if m.get("status") == "stopped"]
    check("S7 被挡回来后旧半截原封不动", bool(still) and still[0]["id"] == (stopped4 or {}).get("id"),
          str([(m["id"], m["status"]) for m in msgs4]))

    return summary()


if __name__ == "__main__":
    sys.exit(main())
