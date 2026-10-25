"""知识库检索工具。"""
from __future__ import annotations

from ... import rag
from ..executor import ToolError, register


@register("rag_query")
async def rag_query_tool(ctx: dict, tool_input: dict) -> dict:
    query = str(tool_input.get("query") or "").strip()
    if not query:
        raise ToolError("rag_query 需要 query 参数")
    user_id = ctx.get("user_id") or ""
    if not user_id:
        raise ToolError("缺少用户上下文")
    collection_ids = tool_input.get("collection_ids")
    if not isinstance(collection_ids, list) or not collection_ids:
        collection_ids = list(ctx.get("rag_collections") or [])
    if not collection_ids:
        raise ToolError("这个任务没有绑定知识库，先在输入框里选一个")
    top_k = int(tool_input.get("top_k") or 5)
    hits = rag.search(collection_ids, user_id, query, top_k)
    if not hits:
        return {"query": query, "hits": [], "text": "知识库里没有匹配的内容。"}
    return {
        "query": query,
        "hits": [{"n": h["n"], "doc": h["doc_name"], "heading": h["heading"],
                  "score": h["score"]} for h in hits],
        "text": rag.format_context(hits),
    }
