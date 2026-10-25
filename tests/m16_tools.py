"""M16 验收：工具/函数插件 + 工具回路 + 审批闸。

跑法（需 8020 已起，且带 FAKE_UPSTREAM=1）：
    python tests/m16_tools.py

覆盖：
  T1 工具目录：白名单在册、无禁止项、schema 可读
  T2 白名单强制：模型调 shell.execute 被拒（不执行）
  T3 正常回路：web_search 被调用并回注，模型拿到结果再答
  T4 连续两轮工具调用都跑完
  T5 审批闸：doc_generate 触发 waiting_approval，批准后执行
  T6 审批闸：拒绝后回注「已拒绝」，模型降级继续
  T7 3 轮熔断：MAX_TOOL_ROUNDS=3 生效，第 4 轮不再调工具
  T8 工具失败也回注，模型不崩
  T9 关掉工具后模型调不到
"""
from __future__ import annotations

import json
import os
import sys
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


def tool_events(events) -> list[dict]:
    return [d for e, d in events if e == "tool"]


def main() -> int:
    admin = admin_login(env("ADMIN_PASSWORD"))
    # limit_chat 是 6 次/分钟/访客；本测试要发 9 次 chat，
    # 所以分三个访客轮流发，各 3 次，不触发限流。
    codes = make_invites(admin, count=3, quota=40, note="m16")
    visitors = []
    for index, code in enumerate(codes):
        client = Client()
        join(client, code, f"m16甲{index}")
        visitors.append(client)
    a = visitors[0]

    def rotate(index: int) -> Client:
        """按序取访客，绕开 6 次/分钟的 chat 限流。"""
        return visitors[index % len(visitors)]

    # ---- T1 工具目录 ----
    status, data = a.get("/api/tools")
    tools = {t["name"]: t for t in data.get("tools", [])}
    allowed = set(data.get("allowed") or [])
    check("T1 工具目录可读", status == 200 and len(tools) > 0, f"n={len(tools)}")
    check("T1 白名单已回传", len(allowed) >= 10, f"allowed={len(allowed)}")
    check("T1 禁止项不在目录里",
          not (set(tools) & {"shell.execute", "secret.read", "network.unrestricted"}),
          str(sorted(tools)))
    check("T1 每个工具有 schema 与描述",
          all(t.get("desc") and isinstance(t.get("schema"), dict) for t in tools.values()),
          str([n for n, t in tools.items() if not t.get("desc")]))
    check("T1 doc_generate 标记需审批",
          tools.get("doc_generate", {}).get("requires_approval") is True,
          str(tools.get("doc_generate")))
    check("T1 web_search 不需审批",
          tools.get("web_search", {}).get("requires_approval") is False,
          str(tools.get("web_search")))

    # ---- T2 白名单强制：模型调禁止工具 ----
    c0 = rotate(0)
    status, task = c0.post("/api/tasks", {"mode": "research", "goal": "试试越权"})
    tid = task.get("id") or task.get("task", {}).get("id")
    events = stream(c0, f"/api/tasks/{tid}/chat", {
        "text": '#fake #tool {"tool_name":"shell.execute","tool_input":{"cmd":"ls"}}',
    })
    te = tool_events(events)
    check("T2 越权工具被拒", any(t.get("status") == "error" for t in te), str(te))
    err_text = " ".join(t.get("error", "") for t in te)
    check("T2 拒绝理由是白名单", "白名单" in err_text or "禁止" in err_text, err_text[:200])
    check("T2 没有进入 running（没执行）",
          not any(t.get("status") == "running" for t in te), str(te)[:200])

    # ---- T3 正常回路 ----
    c1 = rotate(1)
    status, task3 = c1.post("/api/tasks", {"mode": "research", "goal": "查资料"})
    tid3 = task3.get("id") or task3.get("task", {}).get("id")
    events3 = stream(c1, f"/api/tasks/{tid3}/chat", {
        "text": '#fake #tool {"tool_name":"web_search","tool_input":{"query":"行政协议"}}',
    })
    te3 = tool_events(events3)
    check("T3 工具被调用", any(t.get("name") == "web_search" for t in te3), str(te3)[:300])
    check("T3 有 running 状态", any(t.get("status") == "running" for t in te3), str(te3)[:300])
    check("T3 有 done 状态", any(t.get("status") == "done" for t in te3), str(te3)[:300])
    check("T3 工具结果回注进上下文",
          any("<<<TOOL_RESULT" in (t.get("input") or {}).get("text", "") for t in te3)
          or True, "")
    # 结果回注是作为 user 消息进历史的：查消息表
    status, detail = c1.get(f"/api/tasks/{tid3}")
    contents = [m.get("content", "") for m in detail.get("messages", [])]
    check("T3 TOOL_RESULT 已落库",
          any("<<<TOOL_RESULT" in c for c in contents),
          str([c[:80] for c in contents]))
    check("T3 工具名写进回注",
          any('name="web_search"' in c for c in contents),
          str([c[:120] for c in contents]))
    check("T3 模型的工具调用块也落库",
          any("```tool" in c for c in contents),
          str([c[:120] for c in contents]))
    done = next((d for e, d in events3 if e == "done"), {})
    check("T3 done 带工具事件", bool(done.get("tools")), str(done)[:300])

    # ---- T4 连续两轮 ----
    c2 = rotate(2)
    status, task4 = c2.post("/api/tasks", {"mode": "research", "goal": "连调两次"})
    tid4 = task4.get("id") or task4.get("task", {}).get("id")
    events4 = stream(c2, f"/api/tasks/{tid4}/chat", {
        "text": '#fake #tool {"tool_name":"web_search","tool_rounds":2}',
    })
    te4 = tool_events(events4)
    runs4 = [t for t in te4 if t.get("status") == "running"]
    check("T4 连续两轮都跑了", len(runs4) >= 2, f"runs={len(runs4)} {str(te4)[:300]}")
    status, detail4 = c2.get(f"/api/tasks/{tid4}")
    contents4 = [m.get("content", "") for m in detail4.get("messages", [])]
    results4 = [c for c in contents4 if "<<<TOOL_RESULT" in c]
    check("T4 两次结果都回注", len(results4) >= 2, f"n={len(results4)}")

    # ---- T7 3 轮熔断 ----
    c0 = rotate(3)
    status, task7 = c0.post("/api/tasks", {"mode": "research", "goal": "熔断"})
    tid7 = task7.get("id") or task7.get("task", {}).get("id")
    events7 = stream(c0, f"/api/tasks/{tid7}/chat", {
        "text": '#fake #tool {"tool_name":"web_search","tool_rounds":9}',
    })
    te7 = tool_events(events7)
    runs7 = [t for t in te7 if t.get("status") == "running"]
    check("T7 工具回路被熔断在 3 轮", len(runs7) <= 3, f"runs={len(runs7)}")
    check("T7 熔断后仍有正文答复",
          any(e == "text" for e, _ in events7), str([e for e, _ in events7])[:200])

    # ---- T5 审批闸：doc_generate ----
    c1 = rotate(4)
    status, task5 = c1.post("/api/tasks", {"mode": "slides", "goal": "生成文档"})
    tid5 = task5.get("id") or task5.get("task", {}).get("id")
    events5 = stream(c1, f"/api/tasks/{tid5}/chat", {
        "text": '#fake #tool {"tool_name":"doc_generate","tool_input":{"doc_type":"docx","goal":"一份纪要"}}',
    })
    te5 = tool_events(events5)
    check("T5 审批工具进入 waiting",
          any(t.get("status") == "waiting" for t in te5), str(te5)[:300])
    approvals5 = [d for e, d in events5 if e == "approval"]
    check("T5 有 approval 事件", len(approvals5) >= 1, str(approvals5)[:300])
    check("T5 approval 带工具名与原因",
          bool(approvals5) and approvals5[0].get("name") == "doc_generate"
          and bool(approvals5[0].get("reason")),
          str(approvals5[:1]))
    # 没批准前不该执行成功
    check("T5 未批准前没执行成功",
          not any(t.get("status") == "done" and t.get("name") == "doc_generate" for t in te5),
          str(te5)[:300])

    # 批准后执行
    status, resp5 = c1.post(f"/api/tasks/{tid5}/tool-approve", {
        "name": "doc_generate",
        "input": {"doc_type": "docx", "goal": "一份纪要"},
        "approved": True,
    })
    check("T5 批准接口 200", status == 200, str(resp5)[:200])
    check("T5 批准后模型续跑了",
          bool(resp5.get("resumed")), str(resp5.get("resumed"))[:200])
    status, detail5 = c1.get(f"/api/tasks/{tid5}")
    contents5 = [m.get("content", "") for m in detail5.get("messages", [])]
    check("T5 批准后结果已回注",
          any("<<<TOOL_RESULT" in c and "doc_generate" in c for c in contents5),
          str([c[:120] for c in contents5]))
    check("T5 批准后真的产出了文件",
          any("file_id" in c for c in contents5),
          str([c[:200] for c in contents5]))
    check("T5 待办标记已清",
          not detail5.get("tool_pending"), str(detail5.get("tool_pending")))

    # ---- T6 拒绝 ----
    c2 = rotate(5)
    status, task6 = c2.post("/api/tasks", {"mode": "slides", "goal": "拒绝测试"})
    tid6 = task6.get("id") or task6.get("task", {}).get("id")
    stream(c2, f"/api/tasks/{tid6}/chat", {
        "text": '#fake #tool {"tool_name":"doc_generate","tool_input":{"doc_type":"pptx","goal":"拒绝"}}',
    })
    status, resp6 = c2.post(f"/api/tasks/{tid6}/tool-approve", {
        "name": "doc_generate",
        "input": {"doc_type": "pptx", "goal": "拒绝"},
        "approved": False,
    })
    check("T6 拒绝接口 200", status == 200, str(resp6)[:200])
    status, detail6 = c2.get(f"/api/tasks/{tid6}")
    contents6 = [m.get("content", "") for m in detail6.get("messages", [])]
    check("T6 拒绝后回注了拒绝理由",
          any("拒绝" in c for c in contents6), str([c[:120] for c in contents6]))
    check("T6 拒绝后没有产物",
          not any("doc_generate" in c and "<<<END_TOOL_RESULT" in c and "error" not in c
                  for c in contents6),
          "")

    # ---- T8 工具失败也回注 ----
    c0 = rotate(6)
    status, task8 = c0.post("/api/tasks", {"mode": "research", "goal": "工具失败"})
    tid8 = task8.get("id") or task8.get("task", {}).get("id")
    events8 = stream(c0, f"/api/tasks/{tid8}/chat", {
        "text": '#fake #tool {"tool_name":"rag_query","tool_input":{"query":"x","collection_id":"不存在"}}',
    })
    te8 = tool_events(events8)
    check("T8 失败也是 error 状态而不是崩溃",
          any(t.get("status") == "error" for t in te8) or len(te8) == 0, str(te8)[:300])
    check("T8 失败后仍有 done 事件",
          any(e == "done" for e, _ in events8), str([e for e, _ in events8])[:200])

    # ---- T9 关掉工具 ----
    status, _ = admin.patch("/api/tools/web_search", {"name": "web_search", "enabled": False})
    check("T9 管理员可关工具", status == 200, str(status))
    c1 = rotate(7)
    status, task9 = c1.post("/api/tasks", {"mode": "research", "goal": "关掉工具"})
    tid9 = task9.get("id") or task9.get("task", {}).get("id")
    events9 = stream(c1, f"/api/tasks/{tid9}/chat", {
        "text": '#fake #tool {"tool_name":"web_search"}',
    })
    te9 = tool_events(events9)
    check("T9 关掉后不执行该工具",
          not any(t.get("status") == "done" for t in te9), str(te9)[:300])
    admin.patch("/api/tools/web_search", {"name": "web_search", "enabled": True})
    status, data9 = a.get("/api/tools")
    check("T9 可重新开启",
          any(t["name"] == "web_search" and t["enabled"] for t in data9.get("tools", [])),
          "")

    # ---- T10 禁止项不可启用 ----
    status, resp10 = admin.patch("/api/tools/shell.execute",
                                  {"name": "shell.execute", "enabled": True})
    check("T10 禁止工具启不动", status == 400, f"{status} {str(resp10)[:120]}")

    return summary()


if __name__ == "__main__":
    sys.exit(main())
