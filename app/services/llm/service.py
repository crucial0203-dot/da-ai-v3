"""统一 LLM 接口：路由层只认这里，不直接碰任何 provider 客户端。

事件协议与原 chat.py 约定一致：(kind, text)，kind ∈ {thinking, text}；
统计写进调用方传入的 result dict。分派依据是 models 注册表里
ModelSpec.provider → PROVIDERS[provider].protocol。

假上游（FAKE_UPSTREAM）在分派之前拦截：只要消息里带 #fake 控制串，
就完全不触网、不花钱——多协议 fake 验收（m13_providers）因此可以离线跑。
"""
from __future__ import annotations

from typing import AsyncIterator

from ... import config
from .providers import minimax_anthropic, openai_compat


def _split(model_id: str | None):
    """model id → (ProviderSpec, api_key, base_url)。key 缺失抛 503 语义的 UpstreamError。

    model 为空时回落注册表默认——管线里的 _complete_json 等旧调用点
    model 参数带空默认值，行为必须与「没传」时一致。
    注册表函数延迟 import：本模块被 llm/__init__ 顶层引用，包还没初始化完。
    """
    from ...services import models  # 延迟 import：models.py 也引用本包，避免循环
    from . import PROVIDERS, provider_api_key, provider_base_url  # noqa: PLC0415

    spec = models.get(model_id or models.default_model_id())
    if not spec:
        raise minimax_anthropic.UpstreamError(400, f"未知模型：{model_id or '(空)'}")
    pspec = PROVIDERS.get(spec.provider)
    if not pspec:
        raise minimax_anthropic.UpstreamError(500, f"模型 {spec.id} 的 provider「{spec.provider}」未注册")
    key = provider_api_key(pspec)
    if not key:
        raise minimax_anthropic.UpstreamError(
            503, f"服务端还没有配置 {pspec.label} 的 Key（{pspec.key_env}），请联系管理员",
        )
    return pspec, key, provider_base_url(pspec)


def check_ready(model_id: str | None) -> None:
    """预扣额度之前调：key 没配就别让用户为注定失败的调用买单。"""
    _split(model_id)


async def stream_chat(
    system: str,
    messages: list[dict],
    *,
    model: str,
    effort: str = "medium",
    max_tokens: int = 8192,
    result: dict | None = None,
) -> AsyncIterator[tuple[str, str]]:
    # result 是调用方持有的统计容器，必须在**同一个 dict** 上初始化。
    # 不能写 `result or {}`：空 dict 是 falsy，会悄悄换成新对象，
    # 调用方收到的永远是空统计（done 不发、额度不落账——m8/m5 回归抓出来的）。
    if result is None:
        result = {}
    result.update({
        "text": "", "thinking": "", "signature": "", "usage": {},
        "stop_reason": None, "model": "",
    })

    if config.FAKE_UPSTREAM:
        from ...services import fake_upstream

        if fake_upstream.control(messages) is not None:
            async for item in fake_upstream.stream_chat(messages, result, model=model):
                yield item
            return

    pspec, key, base_url = _split(model)
    if pspec.protocol == "anthropic-messages":
        async for kind, chunk in minimax_anthropic.stream_chat(
            key, system, messages, effort=effort,
            max_tokens=max_tokens, result=result, model=model,
        ):
            yield kind, chunk
    else:
        async for kind, chunk in openai_compat.stream_chat(
            key, base_url, system, messages,
            max_tokens=max_tokens, result=result, model=model,
        ):
            yield kind, chunk


async def complete(
    system: str,
    messages: list[dict],
    *,
    model: str,
    effort: str = "medium",
    max_tokens: int = 8000,
) -> dict:
    if config.FAKE_UPSTREAM:
        from ...services import fake_upstream

        # FAKE_UPSTREAM 开着就等于「整个上游都是假的」——complete 一律走罐头。
        # 之前只放行带 #fake 控制串 / 文档结构规划 / 记忆评审三种调用，
        # 结果工作流引擎的 LLM 节点（prompt 里没有控制串）漏出去打了真上游，
        # 在限额耗尽的机器上直接 429。控制串仍然有效，只是用来定制罐头内容。
        return await fake_upstream.complete(messages, model=model)

    pspec, key, base_url = _split(model)
    if pspec.protocol == "anthropic-messages":
        return await minimax_anthropic.complete(
            key, system, messages, effort=effort, max_tokens=max_tokens, model=model,
        )
    return await openai_compat.complete(
        key, base_url, system, messages, max_tokens=max_tokens, model=model,
    )


# JSON 修复解析是协议无关的，从这里统一导出，调用方不再依赖具体 provider 模块
from .providers.minimax_anthropic import extract_json  # noqa: E402
