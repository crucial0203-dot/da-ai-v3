"""联网工具：web_search 与 fetch_url。"""
from __future__ import annotations

from .... import config
from ... import web_search
from ..executor import ToolError, register


@register("web_search")
async def web_search_tool(ctx: dict, tool_input: dict) -> dict:
    query = str(tool_input.get("query") or "").strip()
    if not query:
        raise ToolError("web_search 需要 query 参数")
    if not config.WEB_SEARCH_ENABLED:
        raise ToolError("联网检索当前未开启")
    top_k = int(tool_input.get("top_k") or config.WEB_SEARCH_TOP_K)
    try:
        hits, provider = await web_search.search(query, top_k)
    except web_search.WebSearchError as exc:
        raise ToolError(f"检索失败：{exc}") from exc
    if not hits:
        return {"query": query, "provider": provider, "results": [],
                "text": "没有检索到结果。"}
    text = web_search.format_results(hits)
    return {
        "query": query,
        "provider": provider,
        "results": [h.as_dict() for h in hits],
        "text": text,
    }


@register("fetch_url")
async def fetch_url_tool(ctx: dict, tool_input: dict) -> dict:
    url = str(tool_input.get("url") or "").strip()
    if not url:
        raise ToolError("fetch_url 需要 url 参数")
    try:
        body = await web_search.fetch_body(url)
    except web_search.WebSearchError as exc:
        raise ToolError(f"抓取失败：{exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"抓取失败：{type(exc).__name__}") from exc
    return {"url": url, "chars": len(body), "text": body}
