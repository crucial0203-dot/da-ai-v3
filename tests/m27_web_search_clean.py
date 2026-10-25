"""M27 验收：联网搜索 query 清洗（来源乱写 BUG 修复）。

跑法（纯函数直测，不需起服务）：
    python tests/m27_web_search_clean.py

覆盖：
  Q1 clean_query 剥会话外壳，保留实义（「你能不能做PPT」→ 含 PPT）
  Q2 疑问实义词不误杀（「怎么做PPT」「如何做PPT」原样保留）
  Q3 纯客套/无实义 → None（跳过搜索）
  Q4 清洗后真实 Bing 查询返回相关结果（可联网时）
  Q5 bing 摘要不含脚本碎片/代码痕迹
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import check, summary  # noqa: E402
from app.services import web_search  # noqa: E402


def main() -> int:
    cq = web_search.clean_query

    # Q1 剥外壳保实义
    out = cq("你能不能做PPT")
    check("Q1 剥「你能不能」", out is not None and "PPT" in out, repr(out))
    out = cq("你可以帮我写一首诗吗")
    check("Q1 剥「你可以/帮我/吗」", out is not None and "诗" in out and "帮" not in out, repr(out))
    out = cq("麻烦你帮我查一下最近的新闻")
    check("Q1 两层外壳一次剥净", out is not None and "新闻" in out and "帮" not in out, repr(out))
    out = cq("请给我推荐几本历史书")
    check("Q1 剥「请/给我」", out is not None and "历史书" in out, repr(out))

    # Q2 疑问实义词保留
    out = cq("怎么做PPT")
    check("Q2 「怎么做PPT」原样", out == "怎么做PPT", repr(out))
    out = cq("什么是行政协议")
    check("Q2 「什么是行政协议」原样", out == "什么是行政协议", repr(out))
    out = cq("如何准备法考主观题")
    check("Q2 「如何…」原样", out == "如何准备法考主观题", repr(out))
    out = cq("PPT")
    check("Q2 纯英文词直通", out == "PPT", repr(out))
    out = cq("2026 考研大纲变化")
    check("Q2 中英混排直通", out == "2026 考研大纲变化", repr(out))

    # Q3 无实义 → None
    check("Q3 「你能不能」→None", cq("你能不能") is None, repr(cq("你能不能")))
    check("Q3 「谢谢」→None", cq("谢谢") is None, repr(cq("谢谢")))
    check("Q3 「帮我看看吧」→None", cq("帮我看看吧") is None, repr(cq("帮我看看吧")))
    check("Q3 「在吗」→None", cq("在吗") is None, repr(cq("在吗")))
    check("Q3 空串→None", cq("") is None, repr(cq("")))
    check("Q3 「好的」→None", cq("好的") is None, repr(cq("好的")))

    # Q4 真实 Bing：清洗后的 query 与口语整句对比（可联网时才跑）
    try:
        import asyncio
        hits_raw, _ = asyncio.run(web_search.search("你能不能做PPT", top_k=5))
        cleaned = cq("你能不能做PPT") or ""
        hits_clean, _ = asyncio.run(web_search.search(cleaned, top_k=5)) if cleaned else ([], "")
        if hits_raw and hits_clean:
            raw_is_you = (all("你" in h.title for h in hits_raw[:3])
                          and not any("ppt" in h.title.lower() for h in hits_raw))
            clean_relevant = any(("ppt" in h.title.lower()) for h in hits_clean)
            check("Q4 原句直发 Bing 全是「你」的结果（BUG 存在性）", raw_is_you,
                  str([h.title[:30] for h in hits_raw[:3]]))
            check("Q4 清洗后 Bing 返回 PPT 相关结果", clean_relevant,
                  str([h.title[:30] for h in hits_clean[:3]]))
        else:
            print("Q4 跳过（Bing 不可达或无结果）")
    except Exception as exc:  # noqa: BLE001
        print(f"Q4 跳过（联网异常：{exc}）")

    # Q5 摘要不含代码痕迹
    dirty_html = ('<li class="b_algo"><h2><a href="https://example.com/a">正常标题</a></h2>'
                  '<p>正常摘要文字</p></li>'
                  '<script>var x={a:1};function f(){return window.undefined}</script>'
                  '<p>function(e){var t=e.config;return t}</p>')
    import asyncio

    async def fake_get(url, headers=None, timeout=None):
        return dirty_html

    orig = web_search._http_get
    web_search._http_get = fake_get
    try:
        hits = asyncio.run(web_search._search_bing("test", 5))
    finally:
        web_search._http_get = orig
    check("Q5 标题解析正常", len(hits) >= 1 and hits[0].title == "正常标题", str([h.title for h in hits]))
    check("Q5 摘要无脚本碎片", all("function" not in h.snippet and "var " not in h.snippet for h in hits),
          str([h.snippet[:50] for h in hits]))

    return summary()


if __name__ == "__main__":
    raise SystemExit(main())
