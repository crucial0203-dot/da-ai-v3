"""M32 验收：流式部分 JSON（S2-3）——fix-json 移植 + 围栏集成 + 端到端。

覆盖：
  U1 complete_partial_json 与参考实现（assistant-ui fixJson）逐例对拍：
     补全文本 + partialPath 两项都对
  U2 _partial_input 三级降级：整块 / 平衡前缀 / fix-json 补全
  U3 observe() 集成：围栏开着时 input_preview 是部分字段；闭合后为空
  U4 前缀模糊：对一段真实工具 JSON 的每个前缀，补全与预览都不抛异常
  E1 端到端（需 8020 已起且 FAKE_UPSTREAM=1）：模型逐字写围栏，
     SSE 上能收到多个 streaming_args=true 且 input 逐帧变全的工具事件

跑法：
    python tests/m32_partial_json.py            # 全部
    python tests/m32_partial_json.py --unit     # 只跑不需要服务器的部分
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import Client, admin_login, check, join, make_invites, stream, summary  # noqa: E402

from app.services.tools.fence_watch import (  # noqa: E402
    _partial_input,
    complete_partial_json,
    observe,
)

UNIT_ONLY = "--unit" in sys.argv


def _admin_password() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("ADMIN_PASSWORD="):
            return line.split("=", 1)[1].strip()
    return ""

# 参考实现的文档例子（fix-json.ts 头部注释）+ 本次移植时逐行对拍 TS 源码
# 确认过的路径语义（如 INSIDE_NUMBER 的 default 分支会弹栈并弹 path）。
REFERENCE_CASES: list[tuple[str, str, list[str]]] = [
    ('{"foo":[{"a":f', '{"foo":[{"a":false}]}', ["foo", "0"]),
    ('{"foo":', "{}", []),
    ('{"html":"<p>abc', '{"html":"<p>abc"}', ["html"]),
    ('{"name":"artifact_create","input":{"html":"<p>abc',
     '{"name":"artifact_create","input":{"html":"<p>abc"}}', ["input", "html"]),
    ('{"a":12', '{"a":12}', ["a"]),
    ('{"a":12.', '{"a":12}', []),
    ('{"a":tr', '{"a":true}', []),
    ('{"a":"x\\"y', '{"a":"x\\"y"}', ["a"]),
    ('{"a":[1,2', '{"a":[1,2]}', ["a", "1"]),
]

REAL_TOOL_JSON = '{"name": "memory_query", "input": {"query": "用户的偏好是什么"}}'


def unit_tests() -> None:
    print("\n[U1] fix-json 移植对拍（补全文本 + partialPath）")
    for src, want_fixed, want_path in REFERENCE_CASES:
        fixed, path = complete_partial_json(src)
        check(f"补全 {src!r}", fixed == want_fixed, f"got {fixed!r} want {want_fixed!r}")
        check(f"路径 {src!r}", path == want_path, f"got {path} want {want_path}")

    print("\n[U2] _partial_input 三级降级")
    check("整块完整 JSON 直通",
          _partial_input('{"name": "calc", "input": {"expression": "1+1"}}')
          == {"name": "calc", "input": {"expression": "1+1"}})
    check("半截 HTML 被补全救回",
          _partial_input('{"name":"artifact_create","input":{"html":"<p>你好')
          == {"name": "artifact_create", "input": {"html": "<p>你好"}},
          str(_partial_input('{"name":"artifact_create","input":{"html":"<p>你好')))
    check("半截中文字符串也救得回",
          _partial_input('{"input":{"query":"用户的偏') == {"input": {"query": "用户的偏"}})
    check("空正文返回空", _partial_input("") == {})
    check("非 JSON 垃圾返回空（不抛）", _partial_input("这不是 json") == {})

    print("\n[U3] observe() 集成")
    half = '我需要查一下。\n\n```tool\n{"name": "memory_query", "input": {"query": "用户的偏'
    state = observe(half)
    check("围栏开着", state.open is True)
    check("工具名已读出", state.tool_name == "memory_query", state.tool_name)
    check("部分参数可见（query 半个值）",
          state.input_preview == {"name": "memory_query", "input": {"query": "用户的偏"}},
          str(state.input_preview))
    closed = half + '好"}}'
    state2 = observe(closed)
    check("围栏未闭合（还差收尾 ```）时仍是开着", state2.open is True)
    check("写完后 preview 是完整值",
          state2.input_preview == {"name": "memory_query", "input": {"query": "用户的偏好"}},
          str(state2.input_preview))
    fully = closed + "\n```"
    check("围栏闭合后不在进行中", observe(fully).open is False)

    print("\n[U4] 前缀模糊（每个前缀都不抛异常）")
    bad = 0
    for size in range(len(REAL_TOOL_JSON) + 1):
        prefix = REAL_TOOL_JSON[:size]
        if not prefix.strip():
            continue  # 空前缀：complete 返回空串是正确行为，不要求合法 JSON
        try:
            fixed, _ = complete_partial_json(prefix)
            preview = _partial_input(prefix)
            if preview and not isinstance(preview, dict):
                bad += 1
            # 补全结果要么是合法 JSON，要么截断点落在 \u 转义中间（参考
            # 实现同样不保证这种边界，_partial_input 会安全落空）
            try:
                json.loads(fixed)
            except (json.JSONDecodeError, ValueError):
                if "\\u" not in prefix:
                    bad += 1
                    print(f"    可疑前缀 {prefix!r} -> {fixed!r}")
        except Exception as exc:  # noqa: BLE001 —— 模糊测试就是要抓意外
            bad += 1
            print(f"    前缀 {prefix!r} 抛异常: {exc!r}")
    check(f"{len(REAL_TOOL_JSON)} 个非空前缀全部安全", bad == 0, f"bad={bad}")


def e2e_tests() -> None:
    print("\n[E1] 端到端：模型逐字写围栏，SSE 逐帧收到部分参数")
    admin = admin_login(_admin_password())
    codes = make_invites(admin, count=1, quota=30, note="m32")
    client = Client()
    join(client, codes[0], "m32甲")

    status, task = client.post("/api/tasks", {"mode": "writing", "goal": "m32 部分参数"})
    task_id = (task or {}).get("task", {}).get("id") or ""
    check("建任务", bool(task_id), str(task)[:80])

    # fence_char_parts=20：把围栏块按字符切成 ~20 段逐段吐出，
    # 于是 JSON 会在写到一半时被 observe 到 —— 这正是部分解析要捕的状态。
    # 粒度要细到能落在 query 取值中间，10 段会整段跳过。
    prompt = ('#fake {"tool_on": true, "fence_char_parts": 20, '
              '"tool_name": "memory_query", "tool_input": {"query": "用户的偏好是什么"}}')
    events = stream(client, f"/api/tasks/{task_id}/chat", {"text": prompt})

    tool_events = [d for e, d in events if e == "tool"]
    streaming = [d for d in tool_events if d.get("streaming_args")]
    check("收到多个流式工具事件（围栏被逐段观察到）", len(streaming) >= 3,
          f"streaming={len(streaming)} tools={len(tool_events)}")

    with_input = [d for d in streaming if isinstance(d.get("input"), dict) and d.get("input")]
    check("流式事件带上了部分参数（fix-json 补全值）", bool(with_input),
          json.dumps(streaming[:3], ensure_ascii=False)[:200])

    if with_input:
        sizes = [len(json.dumps(d["input"], ensure_ascii=False)) for d in with_input]
        check("部分参数逐帧变全（长度单调不降）",
              all(b >= a for a, b in zip(sizes, sizes[1:])), str(sizes))
        partial_queries = [
            str((d["input"].get("input") or {}).get("query") or "")
            for d in with_input
        ]
        has_half = any(q and q != "用户的偏好是什么" for q in partial_queries)
        check("至少一帧是半截 query（证明捕到写到一半的状态）", has_half,
              str(partial_queries))
        final = [d for d in tool_events if d.get("input", {}).get("input", {}).get("query")
                 == "用户的偏好是什么"]
        check("回路补发完整参数", bool(final), str(len(final)))

    names = [e for e, _ in events]
    check("流以 done 收尾", bool(names) and names[-1] == "done", str(names[-3:]))
    errored = [d for e, d in events if e == "error"]
    check("全程无 error 事件", not errored, str(errored)[:200])


def main() -> int:
    unit_tests()
    if not UNIT_ONLY:
        e2e_tests()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
