"""记忆工具：读用户的记忆。"""
from __future__ import annotations

from ... import memory
from ..executor import ToolError, register


@register("memory_query")
async def memory_query_tool(ctx: dict, tool_input: dict) -> dict:
    query = str(tool_input.get("query") or "").strip()
    user_id = ctx.get("user_id") or ""
    if not user_id:
        raise ToolError("缺少用户上下文")
    items = memory.list_memories(user_id)
    if not items:
        return {"query": query, "memories": [], "text": "还没有记住关于这位用户的任何信息。"}
    if query:
        # 简单关键词过滤
        terms = [t for t in query.lower().split() if t]
        matched = [
            item for item in items
            if not terms or any(t in (item["content"] or "").lower() for t in terms)
        ]
        if matched:
            items = matched
    text = memory.format_context(user_id)
    return {
        "query": query,
        "memories": [{"id": i["id"], "content": i["content"]} for i in items],
        "text": text,
    }
