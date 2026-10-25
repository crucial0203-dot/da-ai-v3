"""示例后端插件「时间工具」—— 注册一个模型可调用的 current_time 工具。

证明：插件不改执行器源码，就能给对话回路加工具（白名单由插件扩展区接管，
FORBIDDEN_TOOLS 仍然硬禁止）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..services.plugins import PluginInfo
from ..services.tools.registry import ToolSpec

PLUGIN = PluginInfo(
    id="builtin.time-tool",
    name="时间工具",
    version="0.1.0",
    description="向对话回路注册 current_time 工具（查询当前时间）",
)


async def _handler(ctx: dict, tool_input: dict[str, Any]) -> Any:
    fmt = str(tool_input.get("format") or "%Y-%m-%d %H:%M:%S")
    try:
        return {"now": datetime.now().strftime(fmt), "timezone": "server-local"}
    except ValueError:
        return {"now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "note": f"格式 {fmt!r} 无效，已回退默认格式"}


def activate(ctx):
    ctx.register_tool(
        ToolSpec(
            "current_time",
            "查询当前时间",
            "返回服务器当前时间。用法：{\"format\":\"%Y-%m-%d %H:%M\"}（可省略）。",
            {"format": {"type": "string", "description": "strftime 格式，可省略"}},
        ),
        _handler,
    )
