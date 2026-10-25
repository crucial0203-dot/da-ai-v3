"""OpenAI 兼容 /chat/completions 协议适配器（智谱 / DeepSeek / Moonshot / 通义）。

与 anthropic-messages 层的关键差异，全部在这里抹平，对上只暴露同一种形态：
- system 是 messages[0]（不是独立字段）
- 鉴权是 Bearer（不是 x-api-key）
- 思考内容在 delta.reasoning_content（没有 signature，历史回带时签名块直接丢）
- usage 字段名不同：prompt_tokens / completion_tokens / prompt_cache_hit_tokens，
  统一映射成 input_tokens / output_tokens / cache_read_input_tokens
- effort 参数没有对应物：调用侧对 effort_supported=False 的模型不该传 effort

历史消息进来是 anthropic content-block 形态（[{"type":"text","text":...}]），
这里压平成纯文本字符串；image block 不可能出现——chat.py 的视觉门禁在更上游，
预填的 openai 系模型 vision=False，纯文本模型收到图片会被拦在门禁那里。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import httpx

from .minimax_anthropic import UpstreamError, _timeout


def _headers(api_key: str, stream: bool) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "authorization": f"Bearer {api_key}",
        "accept": "text/event-stream" if stream else "application/json",
    }


def _flatten(content: Any) -> str:
    """anthropic content-block 数组 → 纯文本。thinking 块丢弃（openai 协议无法回带）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text") or ""
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _payload(
    system: str,
    messages: list[dict],
    max_tokens: int,
    stream: bool,
    model: str | None = None,
) -> dict[str, Any]:
    msgs: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        msgs.append({"role": m.get("role", "user"), "content": _flatten(m.get("content"))})
    body: dict[str, Any] = {
        "model": model,
        "messages": msgs,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if stream:
        # DeepSeek / OpenAI 兼容端点默认流式不回 usage，必须显式要
        body["stream_options"] = {"include_usage": True}
    return body


def _merge_usage(target: dict, usage: dict | None) -> None:
    """openai 命名 → 本项目统一的 anthropic 命名。"""
    if not isinstance(usage, dict):
        return
    mapping = (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
        ("prompt_cache_hit_tokens", "cache_read_input_tokens"),
        ("cached_tokens", "cache_read_input_tokens"),  # openai 原生命名（prompt_tokens_details 内）
    )
    for src, dst in mapping:
        value = usage.get(src)
        if isinstance(value, int) and value and not target.get(dst):
            target[dst] = value
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = details.get("cached_tokens")
        if isinstance(cached, int) and cached and not target.get("cache_read_input_tokens"):
            target["cache_read_input_tokens"] = cached
    reasoning = usage.get("completion_tokens_details") or {}
    if isinstance(reasoning, dict) and reasoning.get("reasoning_tokens"):
        target["thinking_tokens"] = reasoning["reasoning_tokens"]


def _new_result(result: dict | None) -> dict:
    result = result if result is not None else {}
    result.update({
        "text": "", "thinking": "", "signature": "", "usage": {},
        "stop_reason": None, "model": "",
    })
    return result


async def stream_chat(
    api_key: str,
    base_url: str,
    system: str,
    messages: list[dict],
    max_tokens: int = 8192,
    result: dict | None = None,
    model: str | None = None,
) -> AsyncIterator[tuple[str, str]]:
    """逐事件产出 (kind, text)，kind ∈ {thinking, text}，统计写进 result。"""
    result = _new_result(result)
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = _payload(system, messages, max_tokens, stream=True, model=model)

    attempt = 0
    while True:
        attempt += 1
        try:
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                async with client.stream("POST", url, json=payload, headers=_headers(api_key, True)) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        if resp.status_code in (429, 500, 502, 503, 529) and attempt <= 2:
                            await asyncio.sleep(1.2 * attempt)
                            continue
                        raise UpstreamError(resp.status_code, _error_text(resp.status_code, body), body)

                    ctype = (resp.headers.get("content-type") or "").lower()
                    if "application/json" in ctype:
                        # 上游没守流式约定，按整段 JSON 处理
                        raw = (await resp.aread()).decode("utf-8", "replace")
                        parsed = _read_response(json.loads(raw))
                        result.update({k: parsed[k] for k in ("text", "thinking", "usage", "stop_reason")})
                        if parsed["model"]:
                            result["model"] = parsed["model"]
                        if parsed["thinking"]:
                            yield "thinking", parsed["thinking"]
                        if parsed["text"]:
                            yield "text", parsed["text"]
                        return

                    async for kind, text in _iter_sse(resp, result):
                        yield kind, text
                    return
        except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            if attempt <= 2:
                await asyncio.sleep(1.2 * attempt)
                continue
            raise UpstreamError(0, f"连接上游失败：{type(exc).__name__}") from exc


async def _iter_sse(resp: httpx.Response, result: dict) -> AsyncIterator[tuple[str, str]]:
    """openai 流式格式：每个 chunk = {choices:[{delta:{content|reasoning_content}}]}；
    usage 只在最后一个 chunk（需 stream_options.include_usage）；终止符是 data: [DONE]。"""
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        if raw == "[DONE]":
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if data.get("model") and not result.get("model"):
            result["model"] = data["model"]           # 上游回显，验收靠它断"真的换了"
        _merge_usage(result["usage"], data.get("usage"))
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or {}
            if choice.get("finish_reason"):
                result["stop_reason"] = choice["finish_reason"]
            reasoning = delta.get("reasoning_content")
            if reasoning:
                result["thinking"] += reasoning
                yield "thinking", reasoning
            content = delta.get("content")
            if content:
                result["text"] += content
                yield "text", content


async def complete(
    api_key: str,
    base_url: str,
    system: str,
    messages: list[dict],
    max_tokens: int = 8000,
    model: str | None = None,
) -> dict:
    """非流式调用，返回形态与 anthropic 适配器完全一致。"""
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = _payload(system, messages, max_tokens, stream=False, model=model)
    attempt = 0
    while True:
        attempt += 1
        try:
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                resp = await client.post(url, json=payload, headers=_headers(api_key, False))
            if resp.status_code != 200:
                body = resp.text
                if resp.status_code in (429, 500, 502, 503, 529) and attempt <= 2:
                    await asyncio.sleep(1.2 * attempt)
                    continue
                raise UpstreamError(resp.status_code, _error_text(resp.status_code, body), body)
            return _read_response(resp.json())
        except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            if attempt <= 2:
                await asyncio.sleep(1.2 * attempt)
                continue
            raise UpstreamError(0, f"连接上游失败：{type(exc).__name__}") from exc


def _read_response(data: dict) -> dict:
    choices = data.get("choices") or [{}]
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    usage: dict[str, int] = {}
    _merge_usage(usage, data.get("usage"))
    return {
        "text": message.get("content") or "",
        "thinking": message.get("reasoning_content") or "",
        "signature": "",
        "usage": usage,
        "stop_reason": choice.get("finish_reason"),
        "model": data.get("model") or "",
    }


def _error_text(status: int, body: str) -> str:
    hint = {
        400: "请求被上游拒绝（参数或内容不合规）",
        401: "上游鉴权失败，请检查服务器上的 Key 是否有效",
        403: "Key 没有该模型的权限，或账户余额/套餐受限",
        404: "该模型不存在，或服务端地址（BASE_URL）配错了",
        413: "请求体过大",
        429: "上游限流，请稍后再试",
        500: "上游内部错误",
        503: "上游过载或维护中",
    }.get(status, "上游返回错误")
    snippet = ""
    try:
        data = json.loads(body)
        err = data.get("error")
        if isinstance(err, dict):
            snippet = err.get("message") or ""
        elif isinstance(err, str):
            snippet = err
        else:
            snippet = data.get("message") or ""
    except Exception:
        snippet = body[:200]
    return f"{hint}（HTTP {status}）{('：' + snippet) if snippet else ''}"
