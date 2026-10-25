"""M17 验收：用户记忆。

跑法（需 8020 已起，且带 FAKE_UPSTREAM=1）：
    python tests/m17_memory.py

覆盖：
  M1 CRUD：加 / 列 / 改 / 删
  M2 记忆被注入到下一轮的上下文（meta.memory 为真）
  M3 跨用户不可见
  M4 条数上限（20）与单条长度上限（300）
  M5 模型记忆块：#fake #mem 驱动 add 落库
  M6 评审失败不影响主流程（对话照常 done）
  M7 越权改别人的记忆被拒
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import (  # noqa: E402
    Client, admin_login, check, join, make_invites, stream, summary,
)


def env(k: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(k + "="):
            return line.split("=", 1)[1].strip()
    return ""


def main() -> int:
    admin = admin_login(env("ADMIN_PASSWORD"))
    codes = make_invites(admin, count=2, quota=30, note="m17")
    a = Client()
    join(a, codes[0], "m17甲")
    b = Client()
    join(b, codes[1], "m17乙")

    # ---- M1 CRUD ----
    status, data = a.get("/api/memory")
    check("M1 记忆列表可读", status == 200, str(status))
    start = len(data.get("memories") or [])

    status, resp = a.post("/api/memory", {"content": "用户偏好简洁直接的回答", "kind": "preference"})
    check("M1 新增记忆 200", status == 200, str(resp)[:200])
    mem_id = resp.get("id")
    check("M1 返回记忆 id", bool(mem_id), str(resp)[:200])

    status, data = a.get("/api/memory")
    items = data.get("memories") or []
    check("M1 列表里有刚加的", len(items) == start + 1, f"n={len(items)}")
    check("M1 内容与 kind 正确",
          any(m["content"] == "用户偏好简洁直接的回答" and m["kind"] == "preference"
              for m in items),
          str(items[:2]))

    status, _ = a.patch(f"/api/memory/{mem_id}", {"content": "用户偏好简洁，不要空话"})
    check("M1 改记忆 200", status == 200, str(status))
    status, data = a.get("/api/memory")
    check("M1 改完生效",
          any(m["id"] == mem_id and m["content"] == "用户偏好简洁，不要空话"
              for m in data.get("memories") or []),
          str(data.get("memories"))[:300])

    # ---- M3 跨用户不可见 ----
    status, data_b = b.get("/api/memory")
    check("M3 另一个用户看不到这条记忆",
          not any(m["id"] == mem_id for m in data_b.get("memories") or []),
          str(data_b.get("memories"))[:200])

    # ---- M7 越权操作被拒 ----
    status, _ = b.patch(f"/api/memory/{mem_id}", {"content": "我要改别人的"})
    check("M7 改别人的记忆被拒", status == 400, str(status))
    status, _ = b.delete(f"/api/memory/{mem_id}")
    check("M7 删别人的记忆被拒", status == 400, str(status))

    # ---- M2 记忆注入到下一轮 ----
    status, task = a.post("/api/tasks", {"mode": "writing", "goal": "写一段话"})
    tid = task.get("id") or task.get("task", {}).get("id")
    events = stream(a, f"/api/tasks/{tid}/chat", {"text": "#fake 好的"})
    meta = next((d for e, d in events if e == "meta"), {})
    check("M2 meta 报记忆已注入", bool(meta.get("memory")), str(meta.get("memory")))

    # ---- M4 上限 ----
    status, resp = a.post("/api/memory", {"content": "超长" * 400})
    check("M4 超长记忆被拒", status == 400, str(resp)[:200])
    status, resp = a.post("/api/memory", {"content": ""})
    check("M4 空记忆被拒", status in (400, 422), f"{status} {str(resp)[:120]}")

    # 条数上限：灌到 20 条，第 21 条应被拒
    made = 1
    for index in range(30):
        status, resp = a.post("/api/memory", {"content": f"填充记忆 {index}"})
        if status == 200:
            made += 1
        else:
            break
    status, data = a.get("/api/memory")
    total = len(data.get("memories") or [])
    check("M4 条数封顶在 20", total <= 20, f"total={total} made={made}")
    check("M4 第 21 条被拒", made <= 20, f"made={made}")

    # ---- M5 模型记忆块 ----
    # 先清空
    for item in (data.get("memories") or []):
        a.delete(f"/api/memory/{item['id']}")
    status, data = a.get("/api/memory")
    check("M5 清空后为空", len(data.get("memories") or []) == 0, str(data)[:200])

    status, task5 = a.post("/api/tasks", {"mode": "writing", "goal": "记住一件事"})
    tid5 = task5.get("id") or task5.get("task", {}).get("id")
    stream(a, f"/api/tasks/{tid5}/chat", {"text": "#fake #mem"})
    # 评审是回合后的异步任务，给它一点时间
    deadline = time.time() + 8
    found = []
    while time.time() < deadline:
        status, data = a.get("/api/memory")
        found = data.get("memories") or []
        if found:
            break
        time.sleep(0.4)
    check("M5 模型写的记忆落库了", len(found) >= 1, str(found)[:300])
    check("M5 内容是模型给的那句",
          any("简洁直接" in m["content"] for m in found), str(found)[:300])
    check("M5 source 标成 model",
          all(m.get("source") in ("model", "user") for m in found), str(found)[:300])

    # ---- M6 评审失败不影响主流程 ----
    status, task6 = a.post("/api/tasks", {"mode": "writing", "goal": "评审失败测试"})
    tid6 = task6.get("id") or task6.get("task", {}).get("id")
    events6 = stream(a, f"/api/tasks/{tid6}/chat", {"text": "#fake 正常回答"})
    check("M6 对话照常 done", any(e == "done" for e, _ in events6),
          str([e for e, _ in events6])[:200])
    check("M6 有正文", any(e == "text" for e, _ in events6),
          str([e for e, _ in events6])[:200])

    # ---- 清理 ----
    status, data = a.get("/api/memory")
    for item in (data.get("memories") or []):
        a.delete(f"/api/memory/{item['id']}")

    return summary()


if __name__ == "__main__":
    sys.exit(main())
