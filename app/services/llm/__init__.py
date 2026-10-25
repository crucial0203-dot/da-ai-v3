"""多 Provider 注册表：唯一事实源。

协议两种：
- anthropic-messages：MiniMax（/v1/messages，output_config.effort）
- openai-chat：智谱 / DeepSeek / Moonshot / 通义（/chat/completions，Bearer 鉴权）

Key 与 BaseUrl 一律**运行时**从环境变量读（.env 由 run.py 启动时注入），
不做模块级缓存——测试要在 import 之后改 BASE_URL 指向本地 fake，必须生效。

custom provider（OpenAI 兼容自建网关）预留扩展位：加上 spec 与对应
ModelSpec（id 由 OPENAI_COMPATIBLE_1_MODEL 提供）即可启用，暂不开放。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from ... import config  # noqa: F401  （app.config；保持与其它 service 一致的 import 形态）
from .providers.minimax_anthropic import UpstreamError  # 统一导出，调用方只认 llm.UpstreamError
from .service import check_ready, complete, extract_json, stream_chat  # noqa: F401  统一接口入口

__all__ = [
    "ProviderSpec", "PROVIDERS", "UpstreamError",
    "provider", "provider_api_key", "provider_base_url", "provider_ready",
    "available_providers", "stream_chat", "complete", "check_ready", "extract_json",
]


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    protocol: str            # anthropic-messages | openai-chat
    key_env: str
    base_env: str = ""       # 空 = 不允许自定义 base（安全：不能把 key 发到任意地址）
    base_default: str = ""


PROVIDERS: dict[str, ProviderSpec] = {
    "minimax": ProviderSpec(
        "minimax", "MiniMax", "anthropic-messages",
        "MINIMAX_API_KEY", "MINIMAX_BASE_URL", "https://api.minimax.cn/anthropic",
    ),
    "zhipu": ProviderSpec(
        "zhipu", "智谱", "openai-chat",
        "ZHIPU_API_KEY", "ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4",
    ),
    "moonshot": ProviderSpec(
        "moonshot", "月之暗面", "openai-chat",
        "MOONSHOT_API_KEY", "MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1",
    ),
    "deepseek": ProviderSpec(
        "deepseek", "DeepSeek", "openai-chat",
        "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1",
    ),
    "qwen": ProviderSpec(
        "qwen", "通义千问", "openai-chat",
        "QWEN_API_KEY", "QWEN_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    # "custom": ProviderSpec("custom", "自定义", "openai-chat",
    #                        "OPENAI_COMPATIBLE_1_API_KEY", "OPENAI_COMPATIBLE_1_BASE_URL", ""),
}


def provider(provider_id: str) -> ProviderSpec | None:
    return PROVIDERS.get(provider_id)


def provider_api_key(spec: ProviderSpec) -> str:
    return os.environ.get(spec.key_env, "").strip()


def provider_base_url(spec: ProviderSpec) -> str:
    """运行时解析：env 覆盖 > 默认。spec 没开 base_env 就锁死默认值。"""
    if spec.base_env:
        override = os.environ.get(spec.base_env, "").strip().rstrip("/")
        if override:
            return override
    return spec.base_default


def provider_ready(provider_id: str) -> bool:
    spec = PROVIDERS.get(provider_id)
    return bool(spec and provider_api_key(spec))


def available_providers() -> list[dict]:
    """给前端的 provider 概览；无 key 的 available=False（前端隐藏其模型）。"""
    return [
        {
            "id": spec.id,
            "label": spec.label,
            "protocol": spec.protocol,
            "available": bool(provider_api_key(spec)),
        }
        for spec in PROVIDERS.values()
    ]
