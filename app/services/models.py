"""模型注册表：唯一事实源。

白名单**不来自**上游 `GET /v1/models`。2026-10-22 实测该接口返回 9 个 id，
其中 MiniMax-M2.1 / M2.1-highspeed / MiniMax-M2 调用直接 404：

    {"type":"error","error":{"type":"not_found_error",
     "message":"The model \"MiniMax-M2.1\" is no longer available. (2069)"}}

所以这里只列当天实测确认可用的模型。增删都必须先跑 `tests/m8_models.py`。

`vision=True` 的判定标准是「图片真的进了上下文，且模型答对了图中内容」，
不是「接口没报错」。实测 M2.5 / M2.7 收到 image block 会返回 200，但
thinking 里自述 "no image attached"，然后拿文字硬猜 —— 这种一律记为 vision=False。

`effort_supported` 只代表上游接受 `output_config.effort`（6 个模型实测均 200）；
`effort_effective` 代表「档位真的改变 thinking_tokens」，None = 尚未测出结论，
绝不能当 True 用。

V4.0 起每条 ModelSpec 带 provider 字段（→ llm.PROVIDERS 注册表）。
openai-chat 系模型是**预填**（标注 verified_on=""）：id/label 来自各厂商公开
文档，行为未实测，等线上拿到对应 Key 后按 m13 流程逐个验证再改 note；
无 Key 的 provider 一律 available=False，前端隐藏、resolve 时跳过。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .. import config
from .llm import provider_ready

VERIFIED_ON = "2026-10-22"

EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
EFFORT_LABELS: dict[str, str] = {
    "low": "低 · 快",
    "medium": "中",
    "high": "高",
    "xhigh": "很高",
    "max": "最高",
}

IMAGE_EXT = config.IMAGE_EXT


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str                      # 完整展示名
    short: str                      # 胶囊 / 下拉里的极短名
    tier: str                       # flagship | standard | fast
    vision: bool                    # 真的能读图
    effort_supported: bool          # 上游接受 effort 参数
    effort_effective: bool | None   # 档位真的影响思考长度；None = 未测
    note: str                       # 一句实话，前端原样显示
    provider: str = "minimax"       # → llm.PROVIDERS；决定协议与 Key
    tools_supported: bool | None = None   # 原生 tool_use 支持；None = 未探测（N6）
    verified_on: str = VERIFIED_ON


MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        "MiniMax-M3.1-Flash", "MiniMax M3.1-Flash", "M3.1-Flash", "flagship",
        True, True, None, "默认。能读图片和截图，长文结构最稳",
    ),
    ModelSpec(
        "MiniMax-M3", "MiniMax M3", "M3", "flagship",
        True, True, None, "能读图片，回答比默认模型更直接",
    ),
    ModelSpec(
        "MiniMax-M2.5-highspeed", "MiniMax M2.5 极速", "M2.5 极速", "fast",
        False, True, None, "纯文本。最快，适合短问答与改写",
    ),
    ModelSpec(
        "MiniMax-M2.5", "MiniMax M2.5", "M2.5", "standard",
        False, True, None, "纯文本。比极速稳，比默认模型慢",
    ),
    # ---- 预填（verified_on 为空 = 未实测，拿到对应 Key 后先跑 m13 再改） ----
    ModelSpec(
        "glm-4.6", "智谱 GLM-4.6", "GLM-4.6", "flagship",
        False, False, None, "预填未实测。智谱旗舰，需服务端配置 ZHIPU_API_KEY",
        provider="zhipu", verified_on="",
    ),
    ModelSpec(
        "deepseek-chat", "DeepSeek V3", "DS-V3", "standard",
        False, False, None, "预填未实测。通用对话，需服务端配置 DEEPSEEK_API_KEY",
        provider="deepseek", verified_on="",
    ),
    ModelSpec(
        "deepseek-reasoner", "DeepSeek R1", "DS-R1", "flagship",
        False, False, None, "预填未实测。带深度思考，思考内容以 reasoning_content 回传",
        provider="deepseek", verified_on="",
    ),
    ModelSpec(
        "kimi-k2-0905-preview", "Kimi K2", "Kimi K2", "standard",
        False, False, None, "预填未实测。长文本见长，需服务端配置 MOONSHOT_API_KEY",
        provider="moonshot", verified_on="",
    ),
    ModelSpec(
        "qwen-max", "通义千问 Max", "Qwen Max", "standard",
        False, False, None, "预填未实测。需服务端配置 QWEN_API_KEY",
        provider="qwen", verified_on="",
    ),
)

BY_ID: dict[str, ModelSpec] = {m.id: m for m in MODELS}
FALLBACK_ID = MODELS[0].id


def default_model_id() -> str:
    """服务端默认：环境变量 MINIMAX_MODEL 在册就用它，否则回落注册表第一个。

    V4.0 补一条：默认模型的 provider 没 Key 时，按注册表顺序找第一个
    有 Key 的——否则配了 DeepSeek 没配 MiniMax 的部署会永远落在不可用的模型上。
    """
    want = (config.MODEL or "").strip()
    if want in BY_ID and provider_ready(BY_ID[want].provider):
        return want
    for model in MODELS:
        if provider_ready(model.provider):
            return model.id
    return FALLBACK_ID


def get(model_id: str | None) -> ModelSpec | None:
    return BY_ID.get((model_id or "").strip())


def is_vision(model_id: str | None) -> bool:
    spec = get(model_id)
    return bool(spec and spec.vision)


def public_list() -> list[dict]:
    """给 /api/meta 与 /api/models。前端只认这份，不另写一份模型名。

    available=False 的条目（无 Key provider）前端应直接隐藏。
    """
    default = default_model_id()
    out = []
    for model in MODELS:
        item = asdict(model)
        item["default"] = model.id == default
        item["available"] = provider_ready(model.provider)
        out.append(item)
    return out


@dataclass(frozen=True)
class Resolved:
    spec: ModelSpec
    source: str        # request | task | visitor | default
    requested: str     # 前端原始值（可能非法、可能是空）
    fell_back: bool    # 是否发生了「要 A 给了 B」

    def as_dict(self) -> dict:
        return {
            "model": self.spec.id,
            "model_source": self.source,
            "model_requested": self.requested,
            "model_fallback": self.fell_back,
            "vision": self.spec.vision,
        }


def resolve_model(
    requested: str | None = None,
    task_params: dict | None = None,
    visitor: dict | None = None,
) -> Resolved:
    """请求体 → tasks.params_json.model → visitors.pref_model → 注册表默认。

    非法值不抛错、不 500：跳过该来源继续下探，并记下 requested 让前端提示。
    静默把用户换到另一个模型是不允许的，所以 fell_back 一定要回传。

    V4.0：provider 没 Key 的模型等同非法值——跳过下探，不允许 resolve 到
    一个必然 503 的模型上（请求里手填被隐藏的模型 id 也一样）。
    """
    chain = (
        ("request", requested),
        ("task", (task_params or {}).get("model")),
        ("visitor", (visitor or {}).get("pref_model")),
    )
    first_seen = ""
    for source, raw in chain:
        value = raw.strip() if isinstance(raw, str) else ""
        if not value:
            continue
        first_seen = first_seen or value
        spec = BY_ID.get(value)
        if spec and provider_ready(spec.provider):
            return Resolved(spec, source, first_seen, first_seen != value)

    spec = BY_ID[default_model_id()]
    return Resolved(spec, "default", first_seen, bool(first_seen and first_seen != spec.id))


def normalize_effort(value: str | None) -> str | None:
    raw = (value or "").strip().lower()
    return raw if raw in EFFORTS else None
