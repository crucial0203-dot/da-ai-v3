"""V4.0 N6 工具/函数插件。

分四层，权限只能由代码决定，模型说什么都不算数：

1. `registry.py`  —— 平台白名单（硬编码）。不在这里的工具一律拒绝执行。
2. `protocol.py`  —— 文本协议：模型吐 ```tool {...} ``` 块，解析成结构化调用。
3. `executor.py`  —— 执行器：跑白名单里的工具，处理审批闸、超时、结果回注。
4. `builtin/`    —— 内置工具实现（web_search / fetch_url / rag_query / …）。

与旧 `skill_tools.py` 的关系：那份的 ALLOWED/FORBIDDEN 原样搬进 registry 作第一层，
配置文件里的 permissions 只是第二层——两层都放行才执行。
"""
from __future__ import annotations

from . import builtin  # noqa: F401  —— import 即注册内置工具的 handler
from . import kinds
from .executor import (
    ToolError,
    ToolResult,
    execute_tool,
    registered_handlers,
)
from .protocol import ToolCall, format_error, format_result, parse_tool_calls
from .registry import (
    ALLOWED_TOOLS,
    FORBIDDEN_TOOLS,
    ToolSpec,
    assert_allowed,
    builtin_specs,
    catalog_for_prompt,
    tool_spec,
)

__all__ = [
    "ALLOWED_TOOLS", "FORBIDDEN_TOOLS", "ToolSpec",
    "ToolCall", "parse_tool_calls", "format_result", "format_error",
    "ToolError", "ToolResult", "execute_tool", "registered_handlers",
    "assert_allowed", "builtin_specs", "catalog_for_prompt", "tool_spec",
    "kinds",
]
