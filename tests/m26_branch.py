"""M26 验收：P1-5 重生成分支（API 层）。

跑法（需 8020 已起，且带 FAKE_UPSTREAM=1）：
    python tests/m26_branch.py

覆盖：
  S1 首版 parent_id/branch_index 为空（老行不填）
  S2 连点三次重生成 → 三条新消息 parent_id 相同（一次回溯到根）、
     branch_index 依次 0/1/2；首版让位 aborted 但仍在 GET 响应里（切换器数据源）
  S3 三个分支正文互不相同（#fake #vary 每次调用序号递增）
  S4 重生成仍复用原 user 消息：user 消息不新增、上下文那条提问还是原来那句
  S5 分支上再重生成：根不变，新消息接着排 branch_index（根的回溯是递归的）
  S6 GET 只加字段：branch_index 出现在响应里，status/content 等老字段不变
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import Client, admin_login, check, join, make_invites, stream, summary  # noqa: E402


def task_id_of(data: dict) -> str:
    return data.get("id") or data.get("task", {}).get("id") or ""


def msgs_of(client: Client, task_id: str) -> list[dict]:
    _, d = client.get(f"/api/tasks/{task_id}")
    return d.get("messages") or []


def done_of(events) -> list[dict]:
    return [d for e, d in events if e == "done"]


def main() -> int:
    password = ""
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("ADMIN_PASSWORD="):
            password = line.split("=", 1)[1].strip()
    admin = admin_login(password)
    codes = make_invites(admin, count=2, quota=40, note="m26")
    clients = []
    for i, code in enumerate(codes):
        c = Client()
        join(c, code, f"m26分支{i}")
        clients.append(c)

    c0 = clients[0]
    _, task = c0.post("/api/tasks", {"mode": "research", "goal": "m26 分支根"})
    tid = task_id_of(task)

    # ---- S1 首版 ----
    events = stream(c0, f"/api/tasks/{tid}/chat", {"text": "#fake #vary", "web_search": False})
    dones = done_of(events)
    check("S1 首版完成", bool(dones) and dones[0].get("status") == "done", str(dones)[:160])
    msgs = msgs_of(c0, tid)
    assistants = [m for m in msgs if m["role"] == "assistant"]
    check("S1 首版只有一条 assistant", len(assistants) == 1, str(len(assistants)))
    first = assistants[-1]
    first_id = first["id"]
    check("S1 首版 parent_id 为空", first.get("parent_id") is None, str(first.get("parent_id")))
    check("S1 首版 branch_index 为空", first.get("branch_index") is None, str(first.get("branch_index")))
    check("S1 首版带 vary 序号", "（v" in first["content"], first["content"])
    first_content = first["content"]
    users_before = [m for m in msgs if m["role"] == "user"]

    # ---- S2 连点三次重生成 ----
    for i in range(3):
        ev = stream(c0, f"/api/tasks/{tid}/regenerate", {})
        d = done_of(ev)
        check(f"S2 第 {i + 1} 次重生成完成", bool(d) and d[0].get("status") == "done", str(d)[:160])

    msgs = msgs_of(c0, tid)
    assistants = [m for m in msgs if m["role"] == "assistant"]
    branches = sorted([m for m in assistants if m.get("parent_id") == first_id],
                      key=lambda m: m["id"])
    check("S2 三条新分支 parent_id 都指向首版", len(branches) == 3,
          str([(m["id"], m.get("parent_id")) for m in assistants]))
    idx = [m.get("branch_index") for m in branches]
    check("S2 branch_index 依次 0/1/2", idx == [0, 1, 2], str(idx))
    first_now = [m for m in assistants if m["id"] == first_id]
    check("S2 首版让位 aborted", bool(first_now) and first_now[0]["status"] == "aborted",
          str(first_now[0]["status"]) if first_now else "missing")
    check("S2 首版仍在 GET 响应里（切换器数据源）", bool(first_now))
    check("S2 恰好一条 done（其余全让位）",
          len([m for m in assistants if m["status"] == "done"]) == 1,
          str([(m["id"], m["status"]) for m in assistants]))

    # ---- S3 内容互不相同 ----
    contents = [m["content"] for m in branches]
    check("S3 三个分支正文互不相同", len(set(contents)) == 3, json.dumps(contents, ensure_ascii=False))
    check("S3 分支内容与首版不同", all(c != first_content for c in contents))

    # ---- S4 复用 user 消息 ----
    users_after = [m for m in msgs if m["role"] == "user"]
    check("S4 重生成不新增 user 消息", len(users_after) == len(users_before) == 1,
          f"{len(users_before)} -> {len(users_after)}")
    check("S4 user 消息内容不变", users_after[0]["content"] == users_before[0]["content"])

    # ---- S5 分支上再重生成：根不变，序号接着排 ----
    ev = stream(c0, f"/api/tasks/{tid}/regenerate", {})
    check("S5 分支上再重生成完成", bool(done_of(ev)))
    msgs = msgs_of(c0, tid)
    assistants = [m for m in msgs if m["role"] == "assistant"]
    branches = sorted([m for m in assistants if m.get("parent_id") == first_id],
                      key=lambda m: m["id"])
    check("S5 根不变仍指向首版", len(branches) == 4 and all(m["parent_id"] == first_id for m in branches),
          str([(m["id"], m.get("parent_id"), m.get("branch_index")) for m in branches]))
    idx = [m.get("branch_index") for m in branches]
    check("S5 branch_index 接着排 0/1/2/3", idx == [0, 1, 2, 3], str(idx))
    latest = branches[-1]
    check("S5 最新一条是 done", latest["status"] == "done", latest["status"])

    # ---- S6 GET 契约：只加字段 ----
    m0 = assistants[0]
    for field in ("id", "role", "content", "status", "created_at", "plan_flag", "skills"):
        check(f"S6 老字段 {field} 仍在", field in m0)
    check("S6 branch_index 字段在", "branch_index" in m0)

    # ---- S7 第二个任务：单次重生成的 parent_id = 被重生成消息自己的 id ----
    c1 = clients[1]
    _, task2 = c1.post("/api/tasks", {"mode": "research", "goal": "m26 单次重生成"})
    tid2 = task_id_of(task2)
    stream(c1, f"/api/tasks/{tid2}/chat", {"text": "#fake #vary", "web_search": False})
    msgs2 = msgs_of(c1, tid2)
    a2 = [m for m in msgs2 if m["role"] == "assistant"][-1]
    stream(c1, f"/api/tasks/{tid2}/regenerate", {})
    msgs2 = msgs_of(c1, tid2)
    assistants2 = [m for m in msgs2 if m["role"] == "assistant"]
    new2 = [m for m in assistants2 if m["id"] != a2["id"]]
    check("S7 单次重生成产生一条新消息", len(new2) == 1, str(len(new2)))
    if new2:
        check("S7 parent_id = 被重生成消息自己的 id", new2[0].get("parent_id") == a2["id"],
              f"{new2[0].get('parent_id')} vs {a2['id']}")
        check("S7 branch_index = 0", new2[0].get("branch_index") == 0, str(new2[0].get("branch_index")))
        check("S7 旧消息让位 aborted", a2["status"] == "aborted" or
              [m for m in assistants2 if m["id"] == a2["id"]][0]["status"] == "aborted")

    return summary()


if __name__ == "__main__":
    sys.exit(main())
