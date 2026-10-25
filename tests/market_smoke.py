"""插件市场 API 冒烟：目录 → 安装三类插件 → 主题互斥 → 工具注册 → 撤销 → 拒装未知源。

运行（需本地服务已起在 :8020）：
  C:/Users/chenquanda/.workbuddy/binaries/python/envs/default/Scripts/python.exe tests/market_smoke.py
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8020"
PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'  OK ' if ok else '  !! '} {name}{f'  [{detail}]' if detail else ''}")


class Session:
    def __init__(self) -> None:
        self.cookie = ""

    def call(self, path: str, body: dict | None = None) -> tuple[int, dict | str]:
        req = urllib.request.Request(
            BASE + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "Cookie": self.cookie},
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode()
                set_cookie = resp.headers.get("set-cookie")
                if set_cookie:
                    self.cookie = set_cookie.split(";")[0]
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except Exception:  # noqa: BLE001
                return e.code, raw


def env(key: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


def unit_handlers() -> None:
    sys.path.insert(0, str(ROOT))
    from app.services import market  # noqa: WPS433

    import asyncio

    h = market._make_handler({"handler": {"kind": "calc"}})
    r = asyncio.run(h({"expression": "(12.5+3)*2/4"}, {}))
    check("handler.calc", r["result"] == 7.75, str(r))
    try:
        asyncio.run(h({"expression": "__import__('os')"}, {}))
        check("handler.calc 拒绝非法表达式", False)
    except market.MarketError:
        check("handler.calc 拒绝非法表达式", True)

    h2 = market._make_handler({"handler": {"kind": "text_stats"}})
    r2 = asyncio.run(h2({"text": "法律硕士\nLLM"}, {}))
    check("handler.text_stats", r2["chars"] >= 7 and r2["lines"] == 2, str(r2))

    h3 = market._make_handler({"handler": {"kind": "template"}})
    r3 = asyncio.run(h3({"template": "致{who}：{what}", "fields": {"who": "读者", "what": "你好"}}, {}))
    check("handler.template", r3["text"] == "致读者：你好", str(r3))


def unit_tool_wiring() -> None:
    """事实 A 的两处断点回归：
    ① enabled_tools() 漏掉只在内存注册表的插件工具；
    ② catalog_for_prompt() 撞标准 JSON Schema 形状（"object".get 抛 AttributeError）。
    """
    sys.path.insert(0, str(ROOT))
    from app.services.tools import executor, registry

    name = "m24_probe_tool"
    spec = registry.ToolSpec(
        name=name,
        label="探针工具",
        desc="冒烟用",
        # 故意用标准 JSON Schema 形状——这正是以前会炸的那种
        schema={"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
    )
    # 脚本可重复跑：先清残留
    executor._HANDLERS.pop(name, None)
    try:
        registry.unregister_plugin_tool(name)
    except Exception:  # noqa: BLE001
        pass

    registry.register_plugin_tool(spec)
    executor._HANDLERS[name] = lambda args, ctx: {"ok": True}
    try:
        names = executor.enabled_tools("m24-probe-user")
        check("enabled_tools 含插件工具", name in names, str(names))
        text = registry.catalog_for_prompt([name])
        check("catalog_for_prompt 支持 JSON Schema", "expression" in text and name in text, text[:160])
        flat = registry.catalog_for_prompt(["doc_generate"])
        check("catalog_for_prompt 扁平式回归", "doc_type" in flat, flat[:160])
    finally:
        executor._HANDLERS.pop(name, None)
        registry.unregister_plugin_tool(name)

    from app.services import market

    bad = {
        "id": "x", "name": "x", "owner": "o", "kind": "tool-pack",
        "category": "tool", "version": "1", "added": "2026-01-01",
        "description": {"zh": "x"},
        "payload": {"tools": [{"name": "t", "handler": {"kind": "does_not_exist"}}]},
    }
    try:
        market._validate_entry(bad)
        check("handler.kind 白名单拦截", False, "没抛异常")
    except market.MarketError as exc:
        check("handler.kind 白名单拦截", "does_not_exist" in exc.message, exc.message)


def main() -> None:
    unit_handlers()
    unit_tool_wiring()

    s = Session()
    admin = Session()
    status, _ = admin.call("/api/admin/login", {"password": env("ADMIN_PASSWORD")})
    check("管理员登录", status == 200, str(status))
    status, inv = admin.call("/api/admin/invites", {"count": 1, "max_uses": 9, "days": 7, "daily_quota": 100, "note": "market-smoke"})
    code = inv["codes"][0] if status in (200, 201) and inv.get("codes") else ""
    check("建邀请码", bool(code), code)
    status, _ = s.call("/api/join", {"code": code, "nickname": "市场冒烟"})
    check("邀请码登录", status == 200, str(status))

    status, cat = s.call("/api/market/catalog")
    check("市场目录", status == 200 and cat.get("count") == 18, f"status={status} count={cat.get('count')}")
    kinds = {p["kind"] for p in cat.get("plugins", [])}
    check("目录含三类插件", kinds == {"theme", "ui-module", "tool-pack"}, str(kinds))

    # 1) 主题安装 + 互斥
    status, r = s.call("/api/market/install", {"id": "theme.deep-space"})
    check("安装主题 deep-space", status == 200 and r.get("active_theme") == "theme.deep-space", str(r))
    status, r = s.call("/api/market/install", {"id": "theme.warm-paper"})
    check("安装主题 warm-paper", status == 200, str(status))
    status, r = s.call("/api/market/toggle", {"id": "theme.warm-paper", "enabled": True})
    check("主题互斥切换", status == 200 and r.get("active_theme") == "theme.warm-paper", str(r))

    # 2) UI 模块安装
    status, r = s.call("/api/market/install", {"id": "ui.daily-quote"})
    check("安装 UI 模块", status == 200, str(r))
    status, det = s.call("/api/market/plugin/ui.daily-quote")
    check("UI 模块源码可取", status == 200 and "export default" in (det.get("payload", {}).get("source") or ""), str(status))

    # 3) 工具包安装 → 工具进目录
    status, r = s.call("/api/market/install", {"id": "tool.calc"})
    check("安装工具包", status == 200, str(r))
    status, plugs = s.call("/api/plugins")
    ids = [p["id"] for p in plugs.get("plugins", [])]
    check("工具包进后端插件清单", "market.tool.calc" in ids, str(ids))

    # 3.5) 市场工具真能被模型看见（事实 A 的接口层回归）
    _, tl = s.call("/api/tools")
    names = [t["name"] for t in (tl.get("tools") or [])]
    check("工具页可见市场工具", "calc" in names, str(names))
    row = next((t for t in (tl.get("tools") or []) if t["name"] == "calc"), None)
    check("市场工具标 source=market", bool(row) and row.get("source") == "market", str(row)[:160])
    check("市场工具 schema 完整", bool(row) and "properties" in (row.get("schema") or {}), str(row)[:160])

    # 4) 停用工具包 → 撤销 → 恢复
    status, r = s.call("/api/market/toggle", {"id": "tool.calc", "enabled": False})
    check("停用工具包", status == 200, str(status))
    # 注意：/api/plugins 看的是**进程级注册**（别的用户还开着就不摘），
    # 单用户视角要看 /api/tools——工具目录与执行按用户门禁。
    # 本用户是唯一安装者时行会直接消失（注册被摘）；有别的用户开着时行仍在、
    # enabled=False。两种形态都算「本用户视角下架」，别依赖 DB 残留状态。
    _, tl2 = s.call("/api/tools")
    row2 = next((t for t in (tl2.get("tools") or []) if t["name"] == "calc"), None)
    check("停用后本用户视角下架", row2 is None or row2.get("enabled") is False, str(row2)[:160])
    status, r = s.call("/api/market/undo", {})
    # r 可能是 dict（正常）也可能是 str（错误响应体）——别在断言里再炸一次
    check("撤销停用", status == 200, r.get("message", "") if isinstance(r, dict) else str(r)[:120])
    _, tl3 = s.call("/api/tools")
    row3 = next((t for t in (tl3.get("tools") or []) if t["name"] == "calc"), None)
    check("撤销后本用户视角恢复", (row3 or {}).get("enabled") is True, str(row3)[:160])

    # 5) 卸载 + 拒装未知源
    status, r = s.call("/api/market/uninstall", {"id": "ui.daily-quote"})
    check("卸载 UI 模块", status == 200, str(status))
    status, r = s.call("/api/market/install", {"id": "evil.not-in-catalog"})
    check("拒装目录外插件", status == 404, str(status))

    # 6) 目录状态自洽
    status, cat = s.call("/api/market/catalog")
    installed = {p["id"]: p for p in cat.get("plugins", []) if p["installed"]}
    check("安装状态自洽", set(installed) == {"theme.deep-space", "theme.warm-paper", "tool.calc"}, str(set(installed)))

    print(f"\n{'PASS' if not FAIL else 'FAIL'}  {len(PASS)} ok / {len(FAIL)} failed")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
