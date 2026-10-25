"""工具执行器。

执行链：**白名单强制 → 启用状态 → 审批闸 → 市场门禁 → 超时 → 执行 → 结果回注**。

白名单在代码里，审批闸在市场门禁都在服务端——模型和用户都不能绕过。
`requires_approval` 的工具在本轮不执行，而是抛 `ApprovalRequired`，
由 chat.py 落成 `waiting_approval` 状态，等前端批准后 resume。

市场门禁（2026-10-25 补）：目录侧 `enabled_tools()` 早就按用户过滤，执行侧
原先不过滤——工作流节点直调本函数时，A 用户装的工具包会被 B 用户按名调用。
现在两侧一致：市场工具必须 ctx.user_id 装过且启用着。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ... import config
from .registry import (
    ALLOWED_TOOLS,
    FORBIDDEN_TOOLS,
    PLUGIN_SPECS,
    ToolSpec,
    assert_allowed,
    tool_spec,
)


class ToolError(Exception):
    """工具执行失败。message 直接回注给模型。"""

    def __init__(self, message: str, code: str = "tool_error"):
        super().__init__(message)
        self.message = message
        self.code = code


class ApprovalRequired(Exception):
    """需要人工批准。pending 里带工具名与参数。"""

    def __init__(self, name: str, tool_input: dict, reason: str = ""):
        super().__init__(f"工具 {name} 需要批准")
        self.name = name
        self.tool_input = tool_input
        self.reason = reason


@dataclass
class ToolResult:
    name: str
    ok: bool
    output: Any = ""
    error: str = ""
    needs_approval: bool = False
    meta: dict = field(default_factory=dict)

    def as_payload(self) -> dict:
        if self.ok:
            return {"ok": True, "output": self.output, **({"meta": self.meta} if self.meta else {})}
        return {"ok": False, "error": self.error}


# 实际执行函数的注册表：name -> async (ctx, tool_input) -> Any
_HANDLERS: dict[str, Callable[[dict, dict], Awaitable[Any]]] = {}


def register(name: str) -> Callable:
    """装饰器：注册一个内置工具的处理器。"""
    def deco(fn):
        _HANDLERS[name] = fn
        return fn
    return deco


def register_handler(name: str, fn: Callable[[dict, dict], Awaitable[Any]]) -> Callable[[], None]:
    """公开注册入口（插件用）：挂处理器，返回撤销器。"""
    _HANDLERS[name] = fn

    def undo() -> None:
        _HANDLERS.pop(name, None)

    return undo


def enabled_tools(user_id: str, extra: list[str] | None = None) -> list[str]:
    """当前用户可用的对话工具名。

    平台白名单 ∩ 数据库启用状态 ∩ 请求里额外点名的。

    最后一并补上「插件/市场注册的工具」：它们只在内存注册表里（PLUGIN_SPECS +
    _HANDLERS），从不落 tools 表，不在这里并入就永远进不了模型看到的工具目录——
    表现是「装了工具包，但模型既看不到也调不动」（2026-10-24 修）。
    只做并集、只增不减：内置工具的数据库开关语义完全不变。

    已知简化：插件工具的**注册**是进程级的（PLUGIN_SPECS/_HANDLERS 全局一份），
    但目录与执行两侧都按用户门禁（本文件 _market_gate + 上面的 gate），
    所以「所有用户互见」只影响内存注册，不影响谁能真的调到。
    """
    names: list[str] = []
    try:
        from ... import db

        rows = db.rows("SELECT name FROM tools WHERE enabled = 1")
        names = [r["name"] for r in rows]
    except Exception:
        names = []
    if not names:
        names = [s.name for s in _all_builtin()]
    # 已注册且真有处理器的插件工具才算数——只有 spec 没有 handler 的调了必炸
    plugin_names = [n for n in PLUGIN_SPECS if n in _HANDLERS]
    try:
        from ...services import market

        # 市场工具是进程级注册的（全局一份），但目录与执行都按用户门禁：
        # 这个用户没装或停用了，对他就当不存在——否则「我停用了却还在（因为
        # 别的用户开着）」会表现成停用无效。别的插件工具（如内置 current_time）
        # 不在这道门里。
        gate = market.market_tool_names()
        mine = market.active_tool_names(user_id)
        plugin_names = [n for n in plugin_names if n not in gate or n in mine]
    except Exception:
        pass  # 取不到市场状态就维持原样，别静默摘掉内置插件工具
    names = names + [n for n in plugin_names if n not in names]
    if extra:
        # 别兜底到 names：过滤结果为空时 `[...] or names` 会把"请求传了个不认识的工具名"
        # 放大成"全部工具开放"，与本意正好相反。
        wanted = [e for e in extra if e in ALLOWED_TOOLS]
        if wanted:
            names = [n for n in names if n in wanted]
    return [n for n in names if n in ALLOWED_TOOLS and n not in FORBIDDEN_TOOLS]


def _all_builtin() -> list[ToolSpec]:
    from .registry import builtin_specs

    return builtin_specs()


def _ensure_handlers() -> None:
    """确保内置工具的 handler 已注册。

    `@register` 是模块级副作用：只要 builtin/ 下的模块被 import 过一次，
    handler 才会进 `_HANDLERS`。没人 import 它 = 所有工具都「没有可执行实现」。
    这里做一次惰性兜底，避免漏掉 import 链上的某一环。
    """
    if _HANDLERS:
        return
    try:
        from . import builtin  # noqa: F401  —— import 即注册
    except Exception:  # noqa: BLE001
        pass


def registered_handlers() -> list[str]:
    """当前已注册的 handler 名（诊断用）。"""
    _ensure_handlers()
    return sorted(_HANDLERS)


def _approval_required(spec: ToolSpec, tool_input: dict, ctx: dict) -> bool:
    """审批闸判定（mastra resolveToolApprovalVerdict 的简化移植）。

    优先级：豁免标记 > needs_approval 钩子 > requires_approval 静态标记。
    钩子抛异常时**按需要审批处理（fail closed）**——策略出错不该等于放过执行。
    """
    if getattr(spec, "approval_exempt", False):
        return False
    hook = getattr(spec, "needs_approval", None)
    if callable(hook):
        try:
            return bool(hook(tool_input, ctx))
        except Exception:  # noqa: BLE001 —— 判定失败时收紧而不是放松
            return True
    return bool(spec.requires_approval)


def _market_gate(name: str, ctx: dict) -> str:
    """市场工具的按用户门禁。返回空串表示放行，否则是拒绝原因。

    `enabled_tools()` 一直有这道门（目录侧），但 `execute_tool` 没有——
    工作流节点直调 execute_tool 时，A 用户装的工具包会被 B 用户按名调用
    （2026-10-25 修，参考 deepseek-harness core/tools 的 scope 分层）。
    执行侧补上同一道门，目录与执行才真正一致。
    """
    try:
        from ...services import market

        if name not in market.market_tool_names():
            return ""  # 非市场工具不归这道门管（内置/其他插件工具）
        user_id = (ctx or {}).get("user_id") or ""
        if not user_id:
            return f"工具 {name} 需要已登录的用户上下文"
        if name not in market.active_tool_names(user_id):
            return f"工具 {name} 未安装或已停用"
    except Exception:  # noqa: BLE001 —— 市场模块不可用时别把内置工具一起挡掉
        return ""
    return ""


async def execute_tool(
    name: str,
    tool_input: dict,
    ctx: dict,
    *,
    approved: bool = False,
    perms: dict | None = None,
) -> ToolResult:
    """执行单个工具。绝不抛异常——错误要回注给模型让它自己降级。"""
    _ensure_handlers()
    # 第一层：平台白名单（硬编码）
    if name in FORBIDDEN_TOOLS:
        return ToolResult(name, False, error=f"工具 {name} 被平台禁止")
    if name not in ALLOWED_TOOLS:
        return ToolResult(name, False, error=f"工具 {name} 不在平台白名单内")

    spec = tool_spec(name)
    if spec is None:
        return ToolResult(name, False, error=f"工具 {name} 未实现")

    # 第二层：技能/任务的权限声明
    try:
        assert_allowed(perms or {}, name)
    except PermissionError as exc:
        return ToolResult(name, False, error=str(exc))

    # 第三层：审批闸（判定走 _approval_required：豁免 > 钩子 > 静态标记，fail-closed）
    if not approved and _approval_required(spec, tool_input or {}, ctx):
        return ToolResult(
            name, False, error=f"工具 {name} 需要人工批准后才能执行",
            needs_approval=True, meta={"reason": spec.desc},
        )

    # 第三层半：市场工具按用户门禁（详见 _market_gate 注释）
    gate_error = _market_gate(name, ctx)
    if gate_error:
        return ToolResult(name, False, error=gate_error)

    handler = _HANDLERS.get(name)
    if handler is None:
        return ToolResult(name, False, error=f"工具 {name} 没有可执行实现")

    try:
        output = await asyncio.wait_for(handler(ctx, tool_input or {}), timeout=config.TOOL_TIMEOUT)
    except asyncio.TimeoutError:
        return ToolResult(name, False, error=f"工具 {name} 执行超时（{config.TOOL_TIMEOUT:.0f}s）")
    except ToolError as exc:
        return ToolResult(name, False, error=exc.message)
    except Exception as exc:  # noqa: BLE001
        # 带 message 属性的业务异常（市场 handler 抛的 MarketError 等）把
        # 原文回给模型——只报类型名等于把「表达式含非法字符」这种可行动
        # 信息吞掉，模型看到「MarketError」三个字无从下手。
        detail = getattr(exc, "message", None) or type(exc).__name__
        return ToolResult(name, False, error=f"工具 {name} 执行失败：{detail}")

    return ToolResult(name, True, output=output)


def enabled_tool_names() -> list[str]:
    """数据库里启用的工具（供 UI 展示）。"""
    try:
        from ... import db

        rows = db.rows("SELECT name FROM tools WHERE enabled = 1 ORDER BY name")
        return [r["name"] for r in rows]
    except Exception:
        return [s.name for s in _all_builtin()]


def seed_tools() -> None:
    """把内置工具写进 tools 表（幂等）。"""
    from ... import db

    from .registry import builtin_specs

    now = db.now_iso()
    for spec in builtin_specs():
        db.execute(
            "INSERT INTO tools(name, label, desc, schema_json, enabled, requires_approval, builtin, created_at) "
            "VALUES(?,?,?,?,1,?,1,?) "
            "ON CONFLICT(name) DO UPDATE SET label=excluded.label, desc=excluded.desc, "
            "schema_json=excluded.schema_json, requires_approval=excluded.requires_approval",
            (spec.name, spec.label, spec.desc,
             json.dumps(spec.schema, ensure_ascii=False),
             1 if spec.requires_approval else 0, now),
        )
