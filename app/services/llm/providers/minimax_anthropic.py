"""MiniMax Anthropic 兼容接口客户端（V4.0 起由 llm.service 分派调用）。

S0 实测得出的三条硬事实，写进这里：
1. usage 在 `message_start` 里全是 0，真实用量只在 `message_delta`。
2. 多轮回带 thinking + signature 比剥离更便宜（命中前缀缓存），所以保留最近几轮原文。
3. `thinking` 无法关闭，思考深度只能通过 `output_config.effort` 调。

假上游（FAKE_UPSTREAM）分派在 llm/service.py 统一做——这一层是纯 HTTP 客户端，
方便 tests/m13_providers.py 用罐头服务器逐协议验证。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncIterator, Any

import httpx

from .... import config


class UpstreamError(RuntimeError):
    def __init__(self, status: int, message: str, body: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message
        self.body = body[:800]


def _headers(api_key: str, stream: bool) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "accept": "text/event-stream" if stream else "application/json",
    }


def _payload(
    system: str,
    messages: list[dict],
    effort: str,
    max_tokens: int,
    stream: bool,
    model: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        # model 由调用方解析好传进来；不传则退回 config.MODEL，
        # 保证「忘了传参」时行为与加多模型之前完全一致。
        "model": model or config.MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "stream": stream,
    }
    if effort and effort != "none":
        body["output_config"] = {"effort": effort}
    return body


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(connect=15.0, read=config.UPSTREAM_TIMEOUT, write=60.0, pool=15.0)


def _merge_usage(target: dict, usage: dict | None) -> None:
    if not isinstance(usage, dict):
        return
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and value:
            target[key] = value
    details = usage.get("output_tokens_details")
    if isinstance(details, dict) and details.get("thinking_tokens"):
        target["thinking_tokens"] = details["thinking_tokens"]


async def stream_chat(
    api_key: str,
    system: str,
    messages: list[dict],
    effort: str = "medium",
    max_tokens: int = 8192,
    result: dict | None = None,
    model: str | None = None,
) -> AsyncIterator[tuple[str, str]]:
    """逐事件产出 (kind, text)，kind ∈ {thinking, text}。

    最终统计写进 `result`（调用方传入的 dict），避免异步生成器无法 return 值。
    `result["model"]` 是**上游回显的真实模型名**，用来证明"确实换了模型"。
    """
    result = result if result is not None else {}
    result.update({
        "text": "", "thinking": "", "signature": "", "usage": {},
        "stop_reason": None, "model": "",
    })

    url = f"{provider_base_url()}/v1/messages"
    payload = _payload(system, messages, effort, max_tokens, stream=True, model=model)

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
                            continue  # 早先这里写成 break，会直接跳出 while，重试形同虚设
                        raise UpstreamError(resp.status_code, _error_text(resp.status_code, body), body)

                    # 兜底：上游若按整段 JSON 返回（不守 stream 约定），仍把内容交出去
                    ctype = (resp.headers.get("content-type") or "").lower()
                    if "application/json" in ctype:
                        raw = (await resp.aread()).decode("utf-8", "replace")
                        data = json.loads(raw)
                        parsed = _read_message(data)
                        result.update({k: parsed[k] for k in ("text", "thinking", "signature", "stop_reason")})
                        _merge_usage(result["usage"], data.get("usage"))
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


def provider_base_url() -> str:
    """运行时解析 BASE_URL（注册表在 llm 包里，import 放函数内避免循环）。"""
    from .. import PROVIDERS, provider_base_url as _resolve  # noqa: PLC0415

    return _resolve(PROVIDERS["minimax"])


async def _iter_sse(resp: httpx.Response, result: dict) -> AsyncIterator[tuple[str, str]]:
    event = ""
    async for line in resp.aiter_lines():
        if line.startswith("event:"):
            event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        kind = data.get("type") or event
        if kind == "message_start":
            message = data.get("message") or {}
            if message.get("model"):
                result["model"] = message["model"]     # 上游回显，验收靠它断"真的换了"
            _merge_usage(result["usage"], message.get("usage"))
        elif kind == "content_block_start":
            block = data.get("content_block") or {}
            if block.get("type") == "thinking" and block.get("thinking"):
                result["thinking"] += block["thinking"]
        elif kind == "content_block_delta":
            delta = data.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "thinking_delta" and delta.get("thinking"):
                result["thinking"] += delta["thinking"]
                yield "thinking", delta["thinking"]
            elif dtype == "text_delta" and delta.get("text"):
                result["text"] += delta["text"]
                yield "text", delta["text"]
            elif dtype == "signature_delta" and delta.get("signature"):
                result["signature"] += delta["signature"]
        elif kind == "message_delta":
            _merge_usage(result["usage"], data.get("usage"))
            stop = (data.get("delta") or {}).get("stop_reason")
            if stop:
                result["stop_reason"] = stop
        elif kind == "error":
            err = data.get("error") or {}
            raise UpstreamError(502, err.get("message") or "上游返回错误", json.dumps(data, ensure_ascii=False))
        elif kind == "message_stop":
            return


async def complete(
    api_key: str,
    system: str,
    messages: list[dict],
    effort: str = "medium",
    max_tokens: int = 8000,
    model: str | None = None,
) -> dict:
    """非流式调用，返回 {text, thinking, signature, usage, stop_reason, model}。"""
    url = f"{provider_base_url()}/v1/messages"
    payload = _payload(system, messages, effort, max_tokens, stream=False, model=model)
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
            data = resp.json()
            return _read_message(data)
        except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            if attempt <= 2:
                await asyncio.sleep(1.2 * attempt)
                continue
            raise UpstreamError(0, f"连接上游失败：{type(exc).__name__}") from exc


def _read_message(data: dict) -> dict:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    signature = ""
    for block in data.get("content") or []:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text") or "")
        elif btype == "thinking":
            thinking_parts.append(block.get("thinking") or "")
            signature = block.get("signature") or signature
    usage = data.get("usage") or {}
    merged: dict[str, int] = {}
    _merge_usage(merged, usage)
    return {
        "text": "".join(text_parts),
        "thinking": "".join(thinking_parts),
        "signature": signature,
        "usage": merged,
        "stop_reason": data.get("stop_reason"),
        "model": data.get("model") or "",
    }


def _error_text(status: int, body: str) -> str:
    hint = {
        400: "请求被上游拒绝（参数或内容不合规）",
        401: "上游鉴权失败，请检查服务器上的 Key 是否有效",
        403: "Key 没有该模型的权限，或账户余额/套餐受限",
        404: "该模型已下线或不存在，请在模型浮层换一个",   # 实测 M2.1/M2 会返 2069
        413: "请求体过大",
        429: "上游限流，请稍后再试",
        500: "上游内部错误",
        529: "上游过载",
    }.get(status, "上游返回错误")
    snippet = ""
    try:
        data = json.loads(body)
        err = data.get("error") or {}
        snippet = err.get("message") or data.get("message") or ""
    except Exception:
        snippet = body[:200]
    return f"{hint}（HTTP {status}）{('：' + snippet) if snippet else ''}"


JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


def _balanced_object(text: str) -> str | None:
    """按括号配对取第一个完整 JSON 对象。

    不能只用贪婪正则：模型常在 JSON 之后又写一段解释，正则会把尾巴一起吞掉；
    也不能用非贪婪 `{.*?}`：嵌套对象会在第一个 `}` 就截断。
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _escape_control_in_strings(s: str) -> str:
    """把字符串值里的裸换行/制表符转成转义序列——模型写多行摘要时最常犯这个错，
    报错信息是 "Invalid control character"，但内容本身是好的，不该整条任务失败。"""
    out: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                out.append(ch)
                esc = False
                continue
            if ch == "\\":
                out.append(ch)
                esc = True
                continue
            if ch == '"':
                out.append(ch)
                in_str = False
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_str = True
        out.append(ch)
    return "".join(out)


def _fix_inner_quotes(s: str) -> str:
    """字符串值里夹了英文双引号时补转义。

    判据是"前瞻"：串内遇到 `"`，看后面第一个非空白字符——若是 , : } ] 或结尾，
    说明这是真正的收尾引号；否则它只是正文里的引号，补一个反斜杠。
    中文正文里引用英文词、对话时最常触发，报错是 `Expecting ',' delimiter`。
    """
    out: list[str] = []
    index = 0
    length = len(s)
    in_str = False
    while index < length:
        ch = s[index]
        if in_str and ch == "\\":
            out.append(ch)
            if index + 1 < length:
                out.append(s[index + 1])
                index += 2
                continue
            index += 1
            continue
        if ch == '"':
            if not in_str:
                in_str = True
                out.append(ch)
                index += 1
                continue
            look = index + 1
            while look < length and s[look] in " \t\r\n":
                look += 1
            nxt = s[look] if look < length else ""
            if nxt in ",:}]" or nxt == "":
                in_str = False
                out.append(ch)
            else:
                out.append('\\"')
            index += 1
            continue
        out.append(ch)
        index += 1
    return "".join(out)


def _repair_variants(candidate: str) -> list[str]:
    """按破坏性从小到大给出几个修复版本，谁先解析成功就用谁。"""
    no_trailing = re.sub(r",\s*([}\]])", r"\1", candidate)
    escaped = _escape_control_in_strings(candidate)
    variants = [
        candidate,
        no_trailing,
        escaped,
        _escape_control_in_strings(no_trailing),
        _fix_inner_quotes(_escape_control_in_strings(no_trailing)),
        _fix_inner_quotes(no_trailing),
    ]
    curly = _escape_control_in_strings(no_trailing).replace("“", '"').replace("”", '"')
    variants.append(curly)
    return variants


def extract_json(text: str) -> dict:
    """从模型输出里取出 JSON 对象，尽量不让格式瑕疵毁掉整条任务。

    依次尝试：整段直接解析 → 去围栏 → 括号配对截取 → 若干修复变体。
    全部失败才抛错，由调用方决定是否让模型重写一次。
    """
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    candidates: list[str] = [cleaned]
    balanced = _balanced_object(cleaned)
    if balanced and balanced != cleaned:
        candidates.append(balanced)

    last_error: Exception | None = None
    for candidate in candidates:
        for variant in _repair_variants(candidate):
            try:
                parsed = json.loads(variant)
            except json.JSONDecodeError as exc:
                last_error = exc
                continue
            if isinstance(parsed, dict):
                return parsed
    raise ValueError(f"模型没有返回可解析的 JSON：{last_error}")
