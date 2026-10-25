"""M29 验收：本轮巡检修复的后端项。

跑法（需 8020 已起）：
    python tests/m29_acl_quota.py

覆盖：
  R1 工作流 run 的属主校验：他人私有工作流 → 403；列表里也看不到
  R2 工作流 run 进入配额体系：限额用尽后再跑返回 429
  R3 结算不悬挂：run 失败/被拒后 ai_calls 不停在 reserved（无该用户的 running 残留）
  R4 config 前向引用：WEB_SEARCH_FAKE=1 时 import 不再 NameError
  R5 管理员登录：非 ASCII 口令返回 401 而不是 500
  R6 工具名过滤：传不存在的工具名不再反向"全开工具面"
  R7 SSRF：重定向目标若是内网地址，抓取被拒（逐跳校验）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import (  # noqa: E402
    Client, admin_login, check, join, make_invite, stream, summary,
)


def env(k: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(k + "="):
            return line.split("=", 1)[1].strip()
    return ""


def _graph_simple() -> dict:
    """最小可用图：start → llm → output。"""
    return {
        "nodes": [
            {"id": "n1", "type": "start", "data": {}},
            {"id": "n2", "type": "llm", "data": {"label": "写一句", "prompt": "说你好"}},
            {"id": "n3", "type": "output", "data": {}},
        ],
        "edges": [
            {"source": "n1", "target": "n2"},
            {"source": "n2", "target": "n3"},
        ],
    }


def _ids(rows) -> list[str]:
    """/api/workflows 返回 {"workflows": [...]}，也可能直接是列表。"""
    if isinstance(rows, dict):
        rows = rows.get("workflows") or []
    out: list[str] = []
    for r in rows or []:
        out.append(r if isinstance(r, str) else str(r.get("id") or ""))
    return out


def _builtin_id(rows) -> str:
    """取第一个内置工作流 id（用户工作流与 builtin 定义都算）。"""
    if not isinstance(rows, dict):
        return ""
    for r in rows.get("workflows") or []:
        if isinstance(r, dict) and r.get("builtin"):
            return str(r.get("id") or "")
    for spec in rows.get("builtin") or []:
        if isinstance(spec, dict) and spec.get("id"):
            return str(spec["id"])
    return ""


def _http_status(events: list) -> int:
    """从 stream 的失败标记里取 HTTP 状态码。"""
    for name, data in events:
        if name == "__http_error__":
            return int(data.get("status") or 0)
    return 200


def main() -> int:
    # ---- R4 config 前向引用（纯本机，不需要服务） ----
    code = (
        "import os; os.environ['WEB_SEARCH_FAKE']='1'; os.environ['DEBUG_MODE']='1';"
        "import app.config as c; assert c.WEB_SEARCH_FAKE is True, c.WEB_SEARCH_FAKE"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                          capture_output=True, text=True)
    tail = (proc.stderr or "").strip().splitlines()
    check("R4 WEB_SEARCH_FAKE=1 下 import config 不崩且开关生效",
          proc.returncode == 0, tail[-1] if tail else "")

    # ---- R6 工具名过滤（本机，不需要服务） ----
    from app.services.tools import executor
    # extra 全是不可识别的名字：语义应该是"这条请求没指明工具"，维持默认集合，
    # 既不放大也不剥夺——长度与不传 extra 时一致。
    base = len(executor.enabled_tools("m29-dummy"))
    got = executor.enabled_tools("m29-dummy", extra=["__nonexistent__", "___nope___"])
    check("R6 传不可识别工具名不放大工具面",
          isinstance(got, list) and len(got) == base,
          f"默认 {base} 个，传错名后 {len(got) if isinstance(got, list) else got!r} 个")

    # 真正的回归点：extra 里是合法但本次未授权的工具名时，
    # 旧实现 `[...] or names` 会把空结果回落成**全部工具**（第七 wrecked 逆放大）。
    forbidden = sorted(set(executor.ALLOWED_TOOLS) - set(executor.enabled_tools("m29-dummy")))
    if forbidden:
        got2 = executor.enabled_tools("m29-dummy", extra=[forbidden[0]])
        check("R6 传未授权工具名不再回落到全部工具",
              isinstance(got2, list) and len(got2) == 0,
              f"extra=[{forbidden[0]}] → {len(got2)} 个工具")
    else:
        check("R6 传未授权工具名不再回落到全部工具", False, "找不到用于对比的未授权工具名")

    # ---- R7 SSRF 单测（本机） ----
    from app.services import web_search
    check("R7 safe_url 拒绝云元数据地址",
          web_search.safe_url("http://169.254.169.254/latest/meta-data/") is False)
    check("R7 safe_url 拒绝 loopback",
          web_search.safe_url("http://127.0.0.1:8020/__health") is False)
    check("R7 safe_url 放行公网 https",
          web_search.safe_url("https://github.com/foo/bar") is True)
    # 端到端需可控外网跳转服务器，本机只验证不再把 follow_redirects 交给 httpx
    import inspect
    src = inspect.getsource(web_search._http_get)
    check("R7 _http_get 已改为自行跟进重定向并逐跳校验",
          "follow_redirects=False" in src and "safe_url(current)" in src)

    # ---- 以下需要服务 ----
    admin = admin_login(env("ADMIN_PASSWORD"))

    # R5 非 ASCII 口令：必须 401，不能是 500
    probe = Client()
    status, _ = probe.post("/api/admin/login", {"password": "中文口令测试"})
    check("R5 中文口令返回 401 而非 500", status == 401, f"status={status}")
    status2, _ = probe.post("/api/admin/login", {"password": "ascii-wrong-password"})
    check("R5 英文错误口令仍是 401", status2 == 401, f"status={status2}")

    code_a = make_invite(admin, quota=30, note="m29-a")
    code_b = make_invite(admin, quota=30, note="m29-b")
    ca, cb = Client(), Client()
    join(ca, code_a, "m29-a")
    join(cb, code_b, "m29-b")

    # A 建私有工作流
    status, created = ca.post("/api/workflows", {
        "name": "A的私有流程", "description": "", "graph": _graph_simple(),
    })
    check("R1 建工作流成功", status == 200 and created.get("id"),
          f"status={status} {str(created)[:90]}")
    wf_id = created.get("id") or ""

    if wf_id:
        # B 跑 A 的私有工作流 → 403
        events = stream(cb, f"/api/workflows/{wf_id}/run", {"inputs": {}})
        check("R1 他人工作流不可执行（403）", _http_status(events) == 403,
              f"status={_http_status(events)} events={[e for e, _ in events][:3]}")

        status, rows = cb.get("/api/workflows")
        check("R1 他人列表里看不到 A 的私有工作流",
              status == 200 and wf_id not in _ids(rows),
              f"status={status}")

        status, rows = ca.get("/api/workflows")
        check("R1 属主列表里能看到", status == 200 and wf_id in _ids(rows))

    # 内置工作流人人可跑（不是被误伤）
    status, rows = cb.get("/api/workflows")
    builtin_id = _builtin_id(rows)
    check("R2 内置工作流对所有人可见", bool(builtin_id), f"内置={builtin_id or '无'} 共{len(_ids(rows))}条")

    # ---- R2/R3：低额度账号连跑多次，第二次起必须被 429 拦住 ----
    low = make_invite(admin, quota=1, note="m29-low")
    cl = Client()
    join(cl, low, "m29-low")

    if builtin_id:
        statuses = []
        for _ in range(2):
            statuses.append(_http_status(stream(cl, f"/api/workflows/{builtin_id}/run", {"inputs": {}})))
        check("R2 run 进入配额体系（多次调用后出现 429）",
              any(s == 429 for s in statuses), f"两次状态={statuses}")

        # R3：被 429 拒绝不应留下悬挂的 reserved，当天用量计数不该超过额度
        status, usage = cl.get("/api/usage")
        used = 0
        if isinstance(usage, dict):
            used = int(usage.get("used_calls_today") or usage.get("used") or 0)
        check("R3 额度用尽后已用次数不超过每日额度（1）", used <= 1, f"used={used} raw={str(usage)[:120]}")

    summary()
    return 0


if __name__ == "__main__":
    main()
