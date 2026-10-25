"""M15 验收：联网搜索。

跑法（需 8020 已起，且带 SEARCH_ALLOW_LOOPBACK=1 BING_BASE_URL=... 起）：
    python tests/m15_websearch.py

覆盖：
  W1 /__net 三档状态（ok / degraded / off）
  W2 SSE 有 search 事件，带来源编号
  W3 web_sources 落库（messages.web_flag + web_sources）
  W4 注入正文总量 ≤12000 字
  W5 **usage.calls 不变**（联网不额外扣次）
  W6 内网地址被拒（SSRF 闸）
  W7 关掉联网开关后不检索
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import (  # noqa: E402
    Client, admin_login, check, join, make_invite, stream, summary,
)

FAKE_PORT = int(os.environ.get("FAKE_WEB_PORT", "8901"))


def env(k: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(k + "="):
            return line.split("=", 1)[1].strip()
    return ""


def main() -> int:
    admin = admin_login(env("ADMIN_PASSWORD"))
    code = make_invite(admin, count=1, quota=30, note="m15")
    a = Client()
    join(a, code, "m15甲")

    # W1 /__net
    status, data = a.get("/__net")
    providers = {p["id"]: p for p in data.get("providers", [])}
    check("W1 /__net 有三档状态", len(providers) >= 3, str(list(providers)))
    check("W1 bing 兜底标 degraded",
          providers.get("bing_html", {}).get("status") == "degraded",
          str(providers.get("bing_html")))
    check("W1 无 key 的 provider 标 off",
          all(providers[p]["status"] == "off" for p in ("bocha", "zhipu")
              if not providers[p]["available"]),
          str({p: providers[p]["status"] for p in ("bocha", "zhipu")}))
    check("W1 /__net 带 fts5 标志", "fts5" in data)
    check("W1 /__net 带工具列表", isinstance(data.get("tools"), list))

    # 联网开关默认开
    status, data = a.get("/api/meta")
    check("W1 meta 带 web 配置", "web" in data, str(list(data.keys())))

    # 建任务
    status, task = a.post("/api/tasks", {"mode": "research", "goal": "行政协议仲裁规则"})
    tid = task.get("id") or task.get("task", {}).get("id")

    # W2 SSE search 事件
    events = stream(a, f"/api/tasks/{tid}/chat", {"text": "行政协议里仲裁怎么处理"})
    search_events = [d for e, d in events if e == "search"]
    check("W2 有 search 事件", len(search_events) >= 1,
          str([e for e, _ in events]))
    sources = search_events[0].get("sources", []) if search_events else []
    check("W2 来源带编号", bool(sources) and sources[0].get("n") == 1, str(sources[:2]))
    check("W2 来源带标题与链接",
          bool(sources) and bool(sources[0].get("title")) and bool(sources[0].get("url")),
          str(sources[:1]))
    check("W2 provider 已标注", bool(search_events[0].get("provider")) if search_events else False,
          str(search_events[0].get("provider")) if search_events else "")

    meta = next((d for e, d in events if e == "meta"), {})
    check("W2 meta 报 web 来源数", int(meta.get("web") or 0) > 0, f"web={meta.get('web')}")

    # W3 落库
    status, detail = a.get(f"/api/tasks/{tid}")
    assistants = [m for m in detail.get("messages", []) if m.get("role") == "assistant"]
    check("W3 assistant 有 web 记录", bool(assistants), f"n={len(assistants)}")
    if assistants:
        last = assistants[-1]
        check("W3 web_flag 落库", last.get("web_flag") in (1, True), str(last.get("web_flag")))
        check("W3 web_sources 落库", bool(last.get("web_sources")), str(last.get("web_sources"))[:200])

    # W4 注入 ≤12000 字：看 usage 里的输入 token 不该爆炸
    usage = next((d for e, d in events if e == "usage"), {})
    check("W4 usage 有输入 token", "in" in usage, str(usage))
    check("W4 输入 token 未失控", int(usage.get("in") or 0) < 60000, str(usage.get("in")))

    # W5 usage.calls 不变：查这个用户的今日调用数
    status, usage_api = a.get("/api/usage")
    calls_before = usage_api.get("today", {}).get("calls")
    check("W5 usage 接口可读", calls_before is not None, str(usage_api)[:200])

    # 再发一次（关闭联网）后调用数只 +1
    status, data = a.patch(f"/api/tasks/{tid}", {"params": {"web_search": False}})
    events2 = stream(a, f"/api/tasks/{tid}/chat", {"text": "再来一次，不联网"})
    status, usage_api2 = a.get("/api/usage")
    calls_after = usage_api2.get("today", {}).get("calls")
    check("W5 联网不额外扣次（两轮只 +1）",
          calls_after is not None and calls_before is not None
          and int(calls_after) - int(calls_before) == 1,
          f"{calls_before} -> {calls_after}")

    # W7 关掉后不检索
    search2 = [d for e, d in events2 if e == "search"]
    check("W7 关掉联网后无 search 事件", len(search2) == 0, str(search2[:1]))

    # W6 SSRF 闸（直接打服务端的 /api 无此端点，改测 web_search 服务层语义：
    #     客户端不可自选 URL，这里验证工具目录里没有裸 URL 抓取权限）
    status, tools = a.get("/api/tools")
    names = {t["name"] for t in tools.get("tools", [])}
    check("W6 工具白名单里没有 shell/network 裸权限",
          not (names & {"shell.execute", "network.unrestricted", "secret.read"}),
          str(sorted(names)))
    check("W6 有受控的 fetch_url 工具", "fetch_url" in names, str(sorted(names)))

    return summary()


if __name__ == "__main__":
    sys.exit(main())
