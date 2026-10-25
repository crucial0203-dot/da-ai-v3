"""V4.0 对话上下文装配：RAG / 联网 / 记忆 / 工具目录。

单独一个模块，因为 chat.py 已经够长了——上下文装配的逻辑独立、
可测、且这三块（检索、联网、记忆）都是「有就注入、没有就跳过」。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import config
from . import memory as memory_service
from . import prompts, rag, web_search
from .tools import catalog_for_prompt, executor


@dataclass
class ChatContext:
    """一轮对话用到的全部注入物。"""

    rag_text: str = ""
    rag_hits: list = field(default_factory=list)
    web_text: str = ""
    web_sources: list = field(default_factory=list)
    web_provider: str = ""
    memory_text: str = ""
    tools_text: str = ""
    enabled_tools: list = field(default_factory=list)

    def has_sources(self) -> bool:
        return bool(self.web_sources)

    def as_meta(self) -> dict:
        return {
            "rag": len(self.rag_hits),
            "web": len(self.web_sources),
            "web_provider": self.web_provider,
            "memory": bool(self.memory_text),
            "tools": self.enabled_tools,
        }


def build_context(
    *,
    user_id: str,
    user: dict,
    task_params: dict,
    query: str,
    tool_names: list[str] | None = None,
) -> ChatContext:
    """装配一轮的上下文。

    顺序很重要：先查知识库与联网（要花时间），再拼 memory 与工具目录（纯本地）。
    任一块失败都不阻断对话——降级成「没有这块内容」继续。
    """
    ctx = ChatContext()

    # 工具目录
    try:
        enabled = executor.enabled_tools(user_id, tool_names)
        ctx.enabled_tools = enabled
        if enabled:
            ctx.tools_text = catalog_for_prompt(enabled)
    except Exception:
        ctx.enabled_tools = []
        ctx.tools_text = ""

    # 记忆
    if config.MEMORY_ENABLED:
        try:
            ctx.memory_text = memory_service.format_context(user_id)
        except Exception:
            ctx.memory_text = ""

    # 知识库检索
    if config.RAG_ENABLED:
        collection_ids = task_params.get("rag_collection_ids") or []
        if isinstance(collection_ids, list) and collection_ids:
            try:
                hits = rag.search(collection_ids, user_id, query, config.RAG_TOP_K)
                if hits:
                    ctx.rag_hits = hits
                    ctx.rag_text = rag.format_context(hits)
            except Exception:
                ctx.rag_hits = []
                ctx.rag_text = ""

    return ctx


async def build_context_async(
    *,
    user_id: str,
    user: dict,
    task_params: dict,
    query: str,
    tool_names: list[str] | None = None,
) -> ChatContext:
    """同 build_context，但联网检索是异步的（要等网络）。

    联网失败或超时都不阻断对话——降级成「没有联网结果」继续。
    """
    ctx = build_context(
        user_id=user_id, user=user, task_params=task_params,
        query=query, tool_names=tool_names,
    )
    web_on = bool(task_params.get("web_search"))
    if config.WEB_SEARCH_ENABLED and web_on:
        # 口语整句直发 Bing 会灾难性分词（「你能不能做PPT」→ 全是单字「你」的结果）。
        # 清洗后没有可搜索实义就跳过——诚实无来源好过乱来源。
        cleaned = web_search.clean_query(query) if config.WEB_SEARCH_CLEAN_QUERY else (query or "").strip() or None
        if cleaned:
            try:
                hits, provider = await web_search.search(cleaned)
                if hits:
                    ctx.web_sources = web_search.sources_payload(hits)
                    ctx.web_provider = provider
                    ctx.web_text = web_search.format_results(hits)
                    return ctx
            except Exception:
                pass
        ctx.web_sources = []
        ctx.web_text = ""
        ctx.web_provider = ""
    return ctx


def render_system(mode: str, skills: list[dict], materials_text: str,
                  plan_mode: bool, ctx: ChatContext) -> str:
    """把上下文装配进 system prompt。"""
    return prompts.build_system(
        mode, skills, materials_text,
        plan_mode=plan_mode,
        rag_text=ctx.rag_text,
        memory_text=ctx.memory_text,
        web_text=ctx.web_text,
        tools_text=ctx.tools_text,
    )
