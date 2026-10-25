"""本地联调用的假上游：不花钱、时延可控。

只有在 `DEBUG_MODE=1` 且 `FAKE_UPSTREAM=1` 同时成立时才会被调用（见 llm/service.py），
线上不会走到这里。

行为由用户消息开头的控制串决定，方便一次起服务跑完整套验收：

    #fake                                    默认 6 段、每段 40ms
    #fake {"chunks": 2}                      2 段
    #fake {"delay_ms": 9500, "chunks": 1}    慢到能触发 8 秒心跳
    #fake {"fail": "mid"}                    第 3 段抛上游错误，用来验断流回滚

V4.0 追加的三个能力开关（可叠加，标志位与 JSON 参数可同时出现）：

    #fake #tool {"tool_name":"web_search"}              第一轮吐工具调用块
    #fake #tool {"tool_rounds":2}                      连续两轮都吐工具调用
    #fake #tool {"tool_name":"shell.execute"}          测平台白名单拦截
    #fake #tool {"tool_name":"doc_generate"}           测审批闸（需人工批准）
    #fake #mem                                        吐记忆操作块
    #fake #web                                        注入假联网结果块
    #fake #vary                                       每次调用正文带不同序号（重生成分支差异验收）

注意：JSON 参数在标志位**之后**。标志位是 `#tool` / `#mem` / `#web` / `#vary`，
工具名单独用 `tool_name`（不是 `tool`，`tool` 被标志位占用）。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncIterator

DEFAULT_CHUNKS = 6
DEFAULT_DELAY_MS = 40.0
MARK = "#fake"
# #vary 专用：每次调用 +1，拼进正文。默认关闭——不加标志时输出与从前逐字节一致，
# 现有验收脚本的「第N段。」断言不受影响。开了之后同一 prompt 连调多次正文也不同，
# 用来验「重生成分支之间内容有差异」（m25）。
_VARY_SEQ = 0


def control(messages: list[dict]) -> dict | None:
    """从最近一条用户消息里取控制参数；没有 #fake 标记就返回 None（走真上游）。

    匹配放宽为「包含 #fake」：工作流节点的 prompt 带固定前缀（如「任务目标：」），
    控制串跟在 goal 里，不总在消息开头。

    解析顺序：**先剥标志位，再取 JSON**。反过来会出事：
    `#fake #tool {"tool":"shell.execute"}` 里 JSON 前面有 `#tool`，
    raw_decode 直接失败，参数就丢了。

    另一个坑：工具回路会把 `<<<TOOL_RESULT ...>>>` 块作为 **user 消息**回注到上下文。
    反向扫描时第一条 user 消息往往是这条回注（不含 #fake），
    直接 return None 就走真上游了。所以要**跳过工具回注消息**，
    继续往前找真正带控制串的那条 user 消息。
    """
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        parts = message.get("content") or []
        text = "".join(
            part.get("text") or "" for part in parts if isinstance(part, dict)
        ).strip()
        if "<<<TOOL_RESULT" in text:
            continue  # 工具回注，不是控制串所在处
        idx = text.find(MARK)
        if idx == -1:
            continue
        raw = text[idx + len(MARK):].strip()
        cfg: dict = {}
        # 1) 标志位（tool 只记「开工具模式」，工具名单独用 tool_name）
        for flag in ("tool", "mem", "web", "vary"):
            if re.search(rf"#{flag}\b", raw):
                cfg[flag + "_on"] = True
        # 2) JSON 参数：找第一个 { 开始 raw_decode（后面可能还跟着别的指令文字）
        brace = raw.find("{")
        if brace != -1:
            try:
                parsed, _ = json.JSONDecoder().raw_decode(raw[brace:])
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                cfg.update(parsed)
        return cfg
    return None


def _reset(result: dict) -> None:
    result.update({
        "text": "", "thinking": "", "signature": "",
        "usage": {}, "stop_reason": None, "model": "",
    })


def _split_fence(block: str, parts: int = 4, char_parts: int = 0) -> list[str]:
    """把工具围栏切成 parts 段。切在行边界上，不切断 JSON 中间；
    各段拼回来与原文逐字节一致（split("\n") 再按组 join+\n）。

    char_parts > 0 时改按字符数切（模拟模型逐字写参数）。行边界切法下
    JSON 行是一整块到的，造不出「围栏开着、JSON 写到一半」这个状态——
    S2-3 的 fix-json 部分解析要验的就是这个状态，所以留这个口子。
    """
    if char_parts > 0:
        size = max(1, -(-len(block) // char_parts))  # 向上取整
        return [block[i:i + size] for i in range(0, len(block), size)] or [block]
    lines = block.split("\n")
    if len(lines) < parts:
        return [block]
    per = max(1, len(lines) // parts)
    out: list[str] = []
    for i in range(0, len(lines), per):
        tail = "\n" if i + per < len(lines) else ""
        out.append("\n".join(lines[i:i + per]) + tail)
    return [p for p in out if p]


def _image_count(messages: list[dict]) -> int:
    """数一数 image block 真的进了 messages 没有。

    这是「模型参数透传 + 图片注入」两条链路的零成本验收入口：
    这里数对了，才证明不是只在前端画了个图标。
    """
    total = 0
    for message in messages:
        parts = message.get("content")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "image":
                total += 1
    return total


def _tool_result_count(messages: list[dict]) -> int:
    """上下文里已经回注了几次工具结果。

    每回注一次说明上一轮调过工具；次数用来驱动「连续 N 轮工具调用」的验收。
    """
    count = 0
    for message in messages:
        parts = message.get("content")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and "<<<TOOL_RESULT" in (part.get("text") or ""):
                count += 1
    return count


async def stream_chat(messages: list[dict], result: dict, model: str = "") -> AsyncIterator[tuple[str, str]]:
    from .llm.providers.minimax_anthropic import UpstreamError  # 延迟导入，避免循环引用

    cfg = control(messages) or {}
    chunks = max(1, int(cfg.get("chunks", DEFAULT_CHUNKS)))
    delay = max(0.0, float(cfg.get("delay_ms", DEFAULT_DELAY_MS)) / 1000.0)
    fail_at = cfg.get("fail")
    images = _image_count(messages)

    _reset(result)
    result["model"] = model
    result["vision_blocks"] = images
    await asyncio.sleep(delay)
    if fail_at == "early":
        raise UpstreamError(502, "假上游按指令一开始就报错")

    first = f"假上游 model={model}，收到图 {images} 张。"
    result["thinking"] += first
    yield "thinking", first

    # ---- V4.0：工具调用块 ----
    #
    # `tool_rounds` 决定连着吐几次工具块：回注次数 < tool_rounds 时继续吐，
    # 这样能驱动「连续两轮工具调用」与「3 轮熔断」的验收。
    tool_rounds = max(1, int(cfg.get("tool_rounds", 1)))
    done_rounds = _tool_result_count(messages)
    if cfg.get("tool_on") and done_rounds < tool_rounds:
        name = str(cfg.get("tool_name") or "web_search")
        tool_input = cfg.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {"query": "行政协议诉讼 2024 判例"}
        block = (
            "我需要查一下资料。\n\n"
            "```tool\n"
            + json.dumps({"name": name, "input": tool_input}, ensure_ascii=False)
            + "\n```"
        )
        # fence_ms：工具围栏分段吐出的每段间隔（毫秒）。默认 0 = 整块一次
        # 吐出，与从前逐字节一致（现有验收不受影响）。>0 时模拟模型逐段写
        # 工具调用——P1-7 制品卡的「正在写 + 实时字数」要跨过一个可观测的
        # 窗口才能验收，一次吐完的话 running→done 只有几毫秒。
        # fence_char_parts：按字符数切（造「JSON 写到一半」的流式状态，
        # 见 _split_fence docstring；S2-3 部分 JSON 验收用）。
        fence_ms = max(0.0, float(cfg.get("fence_ms", 0) or 0))
        char_parts = max(0, int(cfg.get("fence_char_parts", 0) or 0))
        pieces = _split_fence(block, char_parts=char_parts) if (fence_ms or char_parts) else [block]
        for piece in pieces:
            if fence_ms:
                await asyncio.sleep(fence_ms / 1000.0)
            result["text"] += piece
            yield "text", piece
        result["signature"] = "fake-signature"
        result["usage"] = {"input_tokens": 120, "output_tokens": 40, "cache_read_input_tokens": 30}
        result["stop_reason"] = "end_turn"
        return

    # ---- V4.0：记忆操作块 ----
    if cfg.get("mem_on"):
        block = (
            "```memory\n"
            + json.dumps({"op": "add", "content": "用户偏好简洁直接的回答，不要空话"},
                         ensure_ascii=False)
            + "\n```"
        )
        result["text"] += block
        yield "text", block
        result["signature"] = "fake-signature"
        result["usage"] = {"input_tokens": 120, "output_tokens": 40}
        result["stop_reason"] = "end_turn"
        return

    vary = bool(cfg.get("vary_on"))
    if vary:
        global _VARY_SEQ
        _VARY_SEQ += 1
        seq = _VARY_SEQ
    for index in range(chunks):
        await asyncio.sleep(delay)
        if fail_at == "mid" and index == 2:
            raise UpstreamError(502, "假上游按指令在第 3 段报错", '{"fake": true}')
        piece = f"第{index + 1}段（v{seq}）。" if vary else f"第{index + 1}段。"
        result["text"] += piece
        yield "text", piece

    result["signature"] = "fake-signature"
    result["usage"] = {"input_tokens": 120, "output_tokens": 40, "cache_read_input_tokens": 30}
    result["stop_reason"] = "end_turn"


async def complete(messages: list[dict], model: str = "") -> dict:
    cfg = control(messages) or {}
    await asyncio.sleep(max(0.0, float(cfg.get("delay_ms", DEFAULT_DELAY_MS)) / 1000.0))
    text = cfg.get("text")
    if not text:
        # 记忆评审要的是 ```memory 块，不是文档结构
        if cfg.get("mem_on") or is_memory_review("", messages):
            text = _memory_block()
        else:
            text = _default_structure()
    return {
        "text": text,
        "thinking": "",
        "signature": "fake-signature",
        "usage": {"input_tokens": 120, "output_tokens": 40},
        "stop_reason": "end_turn",
        "model": model,
        "vision_blocks": _image_count(messages),
    }


def is_memory_review(system: str, messages: list[dict]) -> bool:
    """这次 complete 是不是记忆评审（system 或消息里写着「记忆评审」）。"""
    if "记忆评审" in (system or ""):
        return True
    for message in messages:
        parts = message.get("content")
        text = ""
        if isinstance(parts, list):
            text = "".join(p.get("text") or "" for p in parts if isinstance(p, dict))
        elif isinstance(parts, str):
            text = parts
        if "记忆评审" in text:
            return True
    return False


def _memory_block() -> str:
    return (
        "```memory\n"
        + json.dumps({"op": "add", "content": "用户偏好简洁直接的回答，不要空话"},
                     ensure_ascii=False)
        + "\n```"
    )


def is_structure_plan(system: str, messages: list[dict]) -> bool:
    """这次 complete 调用是不是在求「文档结构」。

    doc_generate 工具内部会自己调一次模型来规划结构，那次调用不带 #fake 控制串
    （控制串只在用户那条消息里）。识别特征：system 里要求「只输出 JSON」
    且提到「结构」。命中就给罐头结构，不去打真上游。
    """
    if "只输出 JSON" in (system or "") and "结构" in (system or ""):
        return True
    for message in messages:
        parts = message.get("content")
        text = ""
        if isinstance(parts, list):
            text = "".join(p.get("text") or "" for p in parts if isinstance(p, dict))
        elif isinstance(parts, str):
            text = parts
        if "只输出 JSON" in text and "结构" in text:
            return True
    return False


def _default_structure() -> str:
    """假上游的默认结构输出。

    doc_generate 工具会按 doc_type 选结构（docx 要 sections，pptx 要 slides），
    但 complete() 拿不到 doc_type，所以两种结构都带上，由调用方取自己需要的。
    """
    return json.dumps({
        "title": "假上游文稿",
        "thesis": "这是假上游生成的主旨句。",
        "subtitle": "这是副标题。",
        "sections": [
            {"heading": "第一节", "paras": ["第一段正文，足够长可以过排版检查。" * 3,
                                           "第二段正文，补充说明与例证。" * 3]},
            {"heading": "第二节", "paras": ["第一段正文，讨论另一层面的问题。" * 3,
                                           "第二段正文，给出小结。" * 3]},
            {"heading": "第三节", "paras": ["第一段正文，收束全文。" * 3]},
        ],
        "slides": [
            {"layout": "cover", "title": "假上游演示", "subtitle": "副标题"},
            {"layout": "bullets", "title": "第一点", "bullets": ["要点一", "要点二", "要点三"]},
            {"layout": "bullets", "title": "第二点", "bullets": ["要点一", "要点二"]},
            {"layout": "two_col", "title": "第三点", "bullets": ["左一", "左二", "右一", "右二"]},
            {"layout": "end", "title": "谢谢"},
        ],
    }, ensure_ascii=False)
