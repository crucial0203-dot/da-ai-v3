"""工具调用的文本协议。

**为什么用文本协议而不是原生 tools 参数**：MiniMax 的 `/v1/messages` 实测不保证支持
原生 tools（探测结论见 tests/probe_minimax_tools.py）。文本协议对任何模型都可用，
代价只是多一轮往返——但一个用户回合只扣一次额度，回路内不再扣。

模型约定的格式：

```tool
{"name": "web_search", "input": {"query": "行政协议诉讼 2024 判例"}}
```

解析要宽容：模型会漏引号、写成单引号、把参数摊平在顶层。坏块不致命——
记进 parse_errors 让模型重写，而不是直接判整轮失败。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# 围栏块：```tool ... ``` / ```tool\n...\n```
TOOL_BLOCK = re.compile(r"```tool\s*\n?([\s\S]*?)\n?```", re.IGNORECASE)
# 宽松变体：```tool {...} ```（同一行）
TOOL_INLINE = re.compile(r"```tool\s+(\{[\s\S]*?\})\s*```", re.IGNORECASE)


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


def _balanced_object(text: str) -> str | None:
    """按括号配对取第一个完整 JSON 对象。

    不用贪婪正则（模型常在 JSON 后又写解释，尾巴会被吞掉）；
    不用非贪婪 `{.*?}`（嵌套对象会在第一个 `}` 截断）。
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


def _repair(raw: str) -> dict[str, Any] | None:
    """尽力把一段文本解析成 dict。失败返回 None。"""
    raw = raw.strip()
    if not raw:
        return None
    candidates = [raw]
    balanced = _balanced_object(raw)
    if balanced and balanced != raw:
        candidates.append(balanced)
    # 单引号 → 双引号（简单场景；值里有撇号的会被跳过，那属于坏块）
    if raw.count("'") and '"' not in raw:
        candidates.append(raw.replace("'", '"'))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _guess_tool(parsed: dict[str, Any]) -> str | None:
    """模型把参数摊平在顶层、漏了 name 时，按 schema 参数键匹配推断工具名。

    实测 M3.1 会照抄工具描述里的「用法」示例直接输出参数对象，
    例如 {"doc_type":"pptx","goal":...} —— 这里按「参数键 ⊆ 工具 schema 键」
    找唯一最大匹配。歧义（平局）时放弃推断，保持保守。
    """
    arg_keys = {
        k for k in parsed
        if k not in {"name", "tool", "function", "input", "arguments", "params"}
    }
    if not arg_keys:
        return None
    from .registry import builtin_specs

    best: str | None = None
    best_score = 0
    tied = False
    for spec in builtin_specs():
        schema_keys = set(spec.schema.keys())
        if not schema_keys or not arg_keys <= schema_keys:
            continue
        score = len(arg_keys)
        if score > best_score:
            best, best_score, tied = spec.name, score, False
        elif score == best_score and spec.name != best:
            tied = True
    return None if tied else best


def parse_tool_calls(text: str) -> tuple[list[ToolCall], list[str]]:
    """从模型正文里抽工具调用。

    返回 (calls, parse_errors)。parse_errors 非空说明有块没解析出来——
    调用方把错误回注给模型让它重写，而不是直接失败。
    """
    calls: list[ToolCall] = []
    errors: list[str] = []
    if not text:
        return calls, errors

    blocks: list[str] = []
    for match in TOOL_BLOCK.finditer(text):
        blocks.append(match.group(1))
    if not blocks:
        for match in TOOL_INLINE.finditer(text):
            blocks.append(match.group(1))

    for raw in blocks:
        parsed = _repair(raw)
        if not parsed:
            errors.append(f"工具块无法解析为 JSON：{raw[:160]}")
            continue
        name = parsed.get("name") or parsed.get("tool") or parsed.get("function")
        if isinstance(name, dict):
            # {"function": {"name": ..., "arguments": {...}}} 这种形态
            args = name.get("arguments") or name.get("input") or {}
            name = name.get("name")
        if not isinstance(name, str) or not name.strip():
            # 容错：参数摊平且缺 name → 按 schema 推断（M3.1 常照抄用法示例）
            guessed = _guess_tool(parsed)
            if guessed:
                name = guessed
            else:
                errors.append(f"工具块缺少 name：{raw[:160]}")
                continue
        name = name.strip()
        tool_input = parsed.get("input")
        if tool_input is None:
            tool_input = parsed.get("arguments")
        if tool_input is None:
            tool_input = parsed.get("params")
        if not isinstance(tool_input, dict):
            # 参数摊平在顶层：把 name 之外的非控制键都收进 input
            tool_input = {
                k: v for k, v in parsed.items()
                if k not in {"name", "tool", "function", "input", "arguments", "params"}
            }
        calls.append(ToolCall(name=name, input=tool_input, raw=raw))
    return calls, errors


def format_result(name: str, payload: Any, call_id: str = "") -> str:
    """工具结果回注格式。

    用独立标记块，模型能明确区分「工具结果」与「用户说的话」。
    """
    if isinstance(payload, str):
        body = payload
    else:
        body = json.dumps(payload, ensure_ascii=False)
    return f'<<<TOOL_RESULT name="{name}" id="{call_id}">>>\n{body}\n<<<END_TOOL_RESULT>>>'


def format_error(name: str, message: str, call_id: str = "") -> str:
    return format_result(name, {"error": message}, call_id)
