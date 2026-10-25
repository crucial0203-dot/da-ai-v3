"""P1-7 流式围栏观察的单元测试。

跑法：python tests/test_fence_watch.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.tools import fence_watch  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  OK  {name}")
    else:
        FAIL.append(name)
        print(f"  !!  {name}  {extra}")


def main() -> int:
    print("\n-- 流式围栏观察 --")

    # 1. 没有围栏
    st = fence_watch.observe("普通正文。")
    check("无围栏 → open=False", st.open is False)

    # 2. 围栏刚开
    st = fence_watch.observe("我来写。\n\n```tool\n")
    check("围栏刚开 → open=True", st.open is True)
    check("围栏刚开 → chars=0", st.chars == 0, str(st.chars))

    # 3. 围栏写了一半（能读出工具名）
    half = "```tool\n{\"name\": \"artifact_create\", \"input\": {\"title\": \"标题\", \"html\": \"<html><body>"
    st = fence_watch.observe(half)
    check("半截围栏 → open=True", st.open is True)
    check("半截围栏 → 读出工具名", st.tool_name == "artifact_create", st.tool_name)
    check("半截围栏 → chars>0", st.chars > 0, str(st.chars))

    # 4. 字符数随流式增长
    a = fence_watch.observe("```tool\n{\"name\":\"artifact_create\",\"input\":{\"html\":\"AAAA")
    b = fence_watch.observe("```tool\n{\"name\":\"artifact_create\",\"input\":{\"html\":\"AAAABBBB")
    check("字符数随流式增长", b.chars > a.chars, f"{a.chars} → {b.chars}")

    # 5. 围栏闭合 → 不在进行中
    closed = half + "\n}}\n```\n后续。"
    st = fence_watch.observe(closed)
    check("围栏闭合 → open=False", st.open is False)

    # 6. 闭合围栏之后再开一个新围栏
    two = closed + "\n再来：\n```tool\n{\"name\":\"web_search\",\"input\":{"
    st = fence_watch.observe(two)
    check("第二个围栏未闭合 → open=True", st.open is True)
    check("第二个围栏 → 工具名 web_search", st.tool_name == "web_search", st.tool_name)

    # 7. 普通代码块里出现 ```tool 字符串 → 不误判
    st = fence_watch.observe("```python\nprint('```tool')\n```")
    check("代码块内的 ```tool 字符串 → 不误判", st.open is False, str(st.tool_name))

    # 8. ```tooljson 不算围栏
    st = fence_watch.observe("```tooljson\n{}\n```")
    check("```tooljson 不算围栏", st.open is False)

    # 9. 完整围栏（闭合）能解析出参数
    import json as _json
    full = "```tool\n" + _json.dumps(
        {"name": "artifact_create", "input": {"title": "T", "html": "<p>x</p>"}},
        ensure_ascii=False,
    ) + "\n```"
    st = fence_watch.observe(full)
    check("完整围栏 → open=False", st.open is False)

    # 10. 半截 JSON 但已平衡的部分能被解析
    #     {"name":"artifact_create","input":{"html":"<p>x</p>"}}  完整但围栏未闭合
    st = fence_watch.observe("```tool\n" + _json.dumps(
        {"name": "artifact_create", "input": {"html": "<p>x</p>"}}, ensure_ascii=False))
    check("围栏未闭合但 JSON 完整 → open=True", st.open is True)
    check("围栏未闭合但 JSON 完整 → 读出工具名", st.tool_name == "artifact_create", st.tool_name)

    # 11. 多个工具名取最后一个围栏的
    st = fence_watch.observe(
        "```tool\n{\"name\":\"web_search\",\"input\":{}}\n```\n```tool\n{\"name\":\"artifact_create\",\"input\":{"
    )
    check("多个围栏取最后一个的工具名", st.tool_name == "artifact_create", st.tool_name)

    print(f"\n== 围栏观察: {len(PASS)} 过 / {len(FAIL)} 败 ==")
    if FAIL:
        for f in FAIL:
            print("  - " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
