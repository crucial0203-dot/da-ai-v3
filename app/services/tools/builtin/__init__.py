"""内置工具实现。

每个工具是一个 `async (ctx, tool_input) -> Any` 的函数，
用 `@register("名字")` 注册到执行器。

**所有工具都必须自己检查权限边界**：ctx 里有 user_id / task_id，
拿不到超出用户范围的数据。执行器只保证「工具名在白名单内」。
"""
from __future__ import annotations

from . import artifacts_tool, doc_tool, memory_tool, rag_tool, web_tool

__all__ = ["web_tool", "rag_tool", "memory_tool", "artifacts_tool", "doc_tool"]
