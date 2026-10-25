"""流式工具围栏观察（P1-7）。

为什么要它：后端原来把整轮上游文本收完才解析工具围栏，于是工具卡的
`running` 事件要等围栏**闭合**才发——从模型开始写工具参数到工具卡出现，
中间有一段「什么都看不到」的空窗。制品卡的「正在写 + 实时字数」就落在
这段空窗里，用户看不到，验收也测不到。

这里改成：文本流里一旦出现 ` ```tool ` 围栏的**开启标记**，就立刻把它
当成一个正在进行的工具调用，边流边报「已写出多少字符」。

思路来源（参考实现）：assistant-ui 的 `ToolCallMessagePart` 把工具参数
定义成**流式部分解析**的 JSON（`args` 在流式期间是半截的），
`useToolArgsStatus` 用来判断哪个字段还在写。本模块是同构的轻量版：
后端边收边解（`complete_partial_json` 就是 assistant-ui `fixJson` 的
Python 移植），前端拿到的就是「当前已写出的参数」——流式期间字段是
半截的，随 SSE 逐帧变全。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# 围栏开启标记必须独占一行（可带前导空白）。这样正文里出现的
# `print('```tool')` 这种字符串不会被误判成围栏。
_TOOL_MARK = re.compile(r"^[ \t]*```tool[ \t]*$", re.IGNORECASE | re.MULTILINE)
_CLOSE_MARK = re.compile(r"^[ \t]*```[ \t]*$", re.MULTILINE)

# 工具名（从半截 JSON 里抠出来）
_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]*)"')


@dataclass
class FenceState:
    """当前正在进行的工具围栏状态。"""

    open: bool = False
    """围栏是否处于「已开启但未闭合」状态。"""

    tool_name: str = ""
    """从半截 JSON 里读到的工具名（可能只读到一半）。"""

    chars: int = 0
    """围栏正文已写出的字符数（含 JSON 结构字符）。"""

    body: str = ""
    """围栏正文原文。"""

    input_preview: dict = field(default_factory=dict)
    """能解析出的部分参数（尽力而为，可能只有部分键）。"""


def _balanced_object(text: str) -> str | None:
    """按括号配对取第一个完整 JSON 对象（半截 JSON 也能取到平衡前缀）。

    截断的 JSON（模型只写了一半）修不了尾，但能取出**已经平衡**的那段：
    比如 `{"name":"artifact_create","input":{"html":"<p>abc` 里，外层
    还没闭合，但 `{"html":"<p>abc` 这段本身也是不平衡的——那就整体取不到。
    所以这里返回 None，交给下面的字段级正则兜底。
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


# ── fix-json 移植（assistant-ui fixJson → Python） ──────────────
#
# 参考实现：assistant-ui packages/assistant-stream/src/utils/json/fix-json.ts
# （MIT License, Copyright (c) 2025 AgentbaseAI Inc. / 2023 Lars Grammel）。
# 一条线性扫描 + 状态栈，把截断的 JSON 补成合法 JSON：
#   '{"foo":[{"a":f'      → '{"foo":[{"a":false}]}'   partialPath=['foo','0']
#   '{"foo":'             → '{}'                      partialPath=[]
#   '{"html":"<p>abc'     → '{"html":"<p>abc"}'       （半截字符串直接闭尾）
# partialPath 是「还在写」的字段路径，前端可据此标写入中。
#
# 状态机与 TS 版一一对应（含 INSIDE_STRING_UNICODE_ESCAPE 的 4 位十六进制
# 重读逻辑）；两处适配 Python：for 循环不能 i--，unicode 转义的重读用
# while + 手工索引；TS 的 path.pop() 空栈返回 undefined，这里返回 None。

_HEX_DIGIT = re.compile(r"[0-9a-fA-F]")

_ROOT = "ROOT"
_FINISH = "FINISH"
_INSIDE_STRING = "INSIDE_STRING"
_INSIDE_STRING_ESCAPE = "INSIDE_STRING_ESCAPE"
_INSIDE_STRING_UNICODE_ESCAPE = "INSIDE_STRING_UNICODE_ESCAPE"
_INSIDE_LITERAL = "INSIDE_LITERAL"
_INSIDE_NUMBER = "INSIDE_NUMBER"
_OBJ_START = "INSIDE_OBJECT_START"
_OBJ_KEY = "INSIDE_OBJECT_KEY"
_OBJ_AFTER_KEY = "INSIDE_OBJECT_AFTER_KEY"
_OBJ_BEFORE_VALUE = "INSIDE_OBJECT_BEFORE_VALUE"
_OBJ_AFTER_VALUE = "INSIDE_OBJECT_AFTER_VALUE"
_OBJ_AFTER_COMMA = "INSIDE_OBJECT_AFTER_COMMA"
_ARR_START = "INSIDE_ARRAY_START"
_ARR_AFTER_VALUE = "INSIDE_ARRAY_AFTER_VALUE"
_ARR_AFTER_COMMA = "INSIDE_ARRAY_AFTER_COMMA"

_VALUE_START_STRING = '"'
_LITERAL_STARTS = ("f", "t", "n")
_DIGITS = "0123456789"


def complete_partial_json(text: str) -> tuple[str, list[str]]:
    """把截断的 JSON 补成合法 JSON，返回 (补全后的文本, 半截字段路径)。"""
    stack: list[str] = [_ROOT]
    last_valid = -1
    literal_start: int | None = None
    unicode_digits = 0
    path: list[str] = []
    current_key: str | None = None

    def push_current_key() -> None:
        nonlocal current_key
        if current_key is not None:
            try:
                path.append(json.loads(f'"{current_key}"'))
            except (json.JSONDecodeError, ValueError):
                path.append(current_key)
            current_key = None

    def pop_path() -> str | None:
        return path.pop() if path else None

    def value_start(char: str, i: int, swap: str) -> None:
        nonlocal last_valid, literal_start, current_key
        if char == _VALUE_START_STRING:
            last_valid = i
            stack.pop()
            stack.extend([swap, _INSIDE_STRING])
            push_current_key()
        elif char in _LITERAL_STARTS:
            last_valid = i
            literal_start = i
            stack.pop()
            stack.extend([swap, _INSIDE_LITERAL])
        elif char == "-":
            stack.pop()
            stack.extend([swap, _INSIDE_NUMBER])
            push_current_key()
        elif char in _DIGITS:
            last_valid = i
            stack.pop()
            stack.extend([swap, _INSIDE_NUMBER])
            push_current_key()
        elif char == "{":
            last_valid = i
            stack.pop()
            stack.extend([swap, _OBJ_START])
            push_current_key()
        elif char == "[":
            last_valid = i
            stack.pop()
            stack.extend([swap, _ARR_START])
            push_current_key()

    def after_object_value(char: str, i: int) -> None:
        # last_valid 必须 nonlocal：Python 里嵌套函数赋值即建局部变量，
        # 少了这行声明，闭合括号就不更新外层的 last_valid——输入以 `}`
        # 结尾时（截断点落在嵌套值闭合后）它会被 text[:last_valid+1]
        # 切掉，补全结果少一个括号（U4 前缀模糊抓到的真 bug）。
        nonlocal current_key, last_valid
        if char == ",":
            stack.pop()
            stack.append(_OBJ_AFTER_COMMA)
        elif char == "}":
            last_valid = i
            stack.pop()
            current_key = pop_path()

    def after_array_value(char: str, i: int) -> None:
        nonlocal current_key, last_valid
        if char == ",":
            stack.pop()
            stack.append(_ARR_AFTER_COMMA)
            current_key = str(int(current_key or "0") + 1)
        elif char == "]":
            last_valid = i
            stack.pop()
            current_key = pop_path()

    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        state = stack[-1]

        if state == _ROOT:
            value_start(char, i, _FINISH)
        elif state == _OBJ_START:
            if char == _VALUE_START_STRING:
                stack.pop()
                stack.append(_OBJ_KEY)
                current_key = ""
            elif char == "}":
                last_valid = i
                stack.pop()
                current_key = pop_path()
        elif state == _OBJ_AFTER_COMMA:
            if char == _VALUE_START_STRING:
                stack.pop()
                stack.append(_OBJ_KEY)
                current_key = ""
        elif state == _OBJ_KEY:
            if char == _VALUE_START_STRING:
                stack.pop()
                stack.append(_OBJ_AFTER_KEY)
            elif char == "\\":
                stack.append(_INSIDE_STRING_ESCAPE)
                current_key = (current_key or "") + char
            else:
                current_key = (current_key or "") + char
        elif state == _OBJ_AFTER_KEY:
            if char == ":":
                stack.pop()
                stack.append(_OBJ_BEFORE_VALUE)
        elif state == _OBJ_BEFORE_VALUE:
            value_start(char, i, _OBJ_AFTER_VALUE)
        elif state == _OBJ_AFTER_VALUE:
            after_object_value(char, i)
        elif state == _INSIDE_STRING:
            if char == _VALUE_START_STRING:
                stack.pop()
                last_valid = i
                current_key = pop_path()
            elif char == "\\":
                stack.append(_INSIDE_STRING_ESCAPE)
            else:
                last_valid = i
        elif state == _ARR_START:
            if char == "]":
                last_valid = i
                stack.pop()
                current_key = pop_path()
            else:
                current_key = "0"
                value_start(char, i, _ARR_AFTER_VALUE)
        elif state == _ARR_AFTER_VALUE:
            if char == ",":
                stack.pop()
                stack.append(_ARR_AFTER_COMMA)
                current_key = str(int(current_key or "0") + 1)
            elif char == "]":
                last_valid = i
                stack.pop()
                current_key = pop_path()
            else:
                last_valid = i
        elif state == _ARR_AFTER_COMMA:
            value_start(char, i, _ARR_AFTER_VALUE)
        elif state == _INSIDE_STRING_ESCAPE:
            stack.pop()
            parent = stack[-1]
            if char == "u":
                stack.append(_INSIDE_STRING_UNICODE_ESCAPE)
                unicode_digits = 0
            elif parent == _INSIDE_STRING:
                last_valid = i
            if parent == _OBJ_KEY:
                current_key = (current_key or "") + char
        elif state == _INSIDE_STRING_UNICODE_ESCAPE:
            parent = stack[-2] if len(stack) >= 2 else _ROOT
            if not _HEX_DIGIT.match(char):
                stack.pop()
                continue  # 重读当前字符（等价 TS 的 i--）
            unicode_digits += 1
            if unicode_digits == 4:
                stack.pop()
                if parent == _INSIDE_STRING:
                    last_valid = i
            if parent == _OBJ_KEY:
                current_key = (current_key or "") + char
        elif state == _INSIDE_NUMBER:
            if char in _DIGITS:
                last_valid = i
            elif char in (",", "}", "]"):
                stack.pop()
                current_key = pop_path()
                if stack[-1] == _ARR_AFTER_VALUE:
                    after_array_value(char, i)
                elif stack[-1] == _OBJ_AFTER_VALUE:
                    after_object_value(char, i)
            else:
                stack.pop()
                current_key = pop_path()
        elif state == _INSIDE_LITERAL:
            partial = text[literal_start:i + 1] if literal_start is not None else ""
            if not ("false".startswith(partial) or "true".startswith(partial) or "null".startswith(partial)):
                stack.pop()
                if stack[-1] == _OBJ_AFTER_VALUE:
                    after_object_value(char, i)
                elif stack[-1] == _ARR_AFTER_VALUE:
                    after_array_value(char, i)
            else:
                last_valid = i

        i += 1

    result = text[:last_valid + 1]
    for state in reversed(stack):
        if state == _INSIDE_STRING:
            result += '"'
        elif state in (_OBJ_KEY, _OBJ_AFTER_KEY, _OBJ_AFTER_COMMA, _OBJ_START,
                       _OBJ_BEFORE_VALUE, _OBJ_AFTER_VALUE):
            result += "}"
        elif state in (_ARR_START, _ARR_AFTER_COMMA, _ARR_AFTER_VALUE):
            result += "]"
        elif state == _INSIDE_LITERAL:
            partial = text[literal_start:] if literal_start is not None else ""
            for word in ("true", "false", "null"):
                if word.startswith(partial):
                    result += word[len(partial):]
                    break
    return result, path


def _partial_input(body: str) -> dict:
    """从半截 JSON 里尽力抠出能确定的参数。

    三条路（依次降级）：
      1) 整块能解析（模型已经写完但围栏还没闭合）→ 直接用
      2) 括号平衡的前缀能解析 → 用前缀
      3) fix-json 补全再解析——救的是 `{"html":"<p>abc` 这类写到一半的
         长字段：不平衡前缀取不到，但闭掉字符串尾就能拿到已写部分
    """
    stripped = body.strip()
    if not stripped:
        return {}
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    balanced = _balanced_object(stripped)
    if balanced:
        try:
            parsed = json.loads(balanced)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    fixed, _partial_path = complete_partial_json(stripped)
    try:
        parsed = json.loads(fixed)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        return {}


def observe(text: str) -> FenceState:
    """观察文本当前所处的工具围栏状态。

    规则：
      - 找到最后一个 ` ```tool ` 开启标记（独占一行）
      - 它后面若没有闭合标记 → 围栏开着（generating 态）
      - 围栏开着时，读出工具名与已写出的字符数
    """
    if not text:
        return FenceState()
    marks = list(_TOOL_MARK.finditer(text))
    if not marks:
        return FenceState()
    mark = marks[-1]
    tail = text[mark.end():]
    # 围栏已闭合 → 不是进行中
    if _CLOSE_MARK.search(tail):
        return FenceState()
    body = tail.lstrip("\r\n")
    # 闭合标记之前的部分就是围栏正文
    close = _CLOSE_MARK.search(body)
    if close:
        body = body[:close.start()]
    name_match = _NAME_RE.search(body)
    tool_name = name_match.group(1) if name_match else ""
    return FenceState(
        open=True,
        tool_name=tool_name,
        chars=len(body),
        body=body,
        input_preview=_partial_input(body),
    )
