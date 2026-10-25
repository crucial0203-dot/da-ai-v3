"""生成文档工具：Word / PPT。

**必须先过人工批准闸**（registry 里 requires_approval=True）——
执行器在批准前不会调到这里。批准后由这里的实现真正出文件。

产出走 V3 已有的 builder（docx_builder / pptx_builder），不重写排版逻辑：
工具只是把「模型的一句话目标」变成「一次真实的文件产出 + 交付物登记」。

注意两个 builder 要的结构不一样：
- docx_builder 要 `sections: [{heading, paras: [str]}]`
- pptx_builder 要 `slides: [{layout, title, bullets|quote|chart}]`
所以结构规划阶段就分开提示，落地时也分开组装。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from .... import config, db
from ... import capabilities, docx_builder, llm
from ...workflow.task_nodes import register_deliverable
from ..executor import ToolError, register

DOCX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document")
PPTX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".presentationml.presentation")


@register("doc_generate")
async def doc_generate_tool(ctx: dict, tool_input: dict) -> dict:
    """生成 Word 或 PPT 文件（已通过审批闸才会到这里）。"""
    doc_type = str(tool_input.get("doc_type") or "docx").lower().strip()
    goal = str(tool_input.get("goal") or "").strip()
    if not goal:
        raise ToolError("doc_generate 需要 goal 参数")
    if doc_type not in {"docx", "pptx"}:
        raise ToolError(f"doc_generate 只支持 docx 与 pptx，收到的是 {doc_type}")

    user_id = ctx.get("user_id") or ""
    task_id = ctx.get("task_id")
    if not user_id:
        raise ToolError("缺少用户上下文")

    capabilities.require("pptx.create" if doc_type == "pptx" else "docx.create")

    structure = await _plan_structure(doc_type, goal)
    if not structure:
        raise ToolError("没能生成可用的文档结构")

    def _build():
        out_path = config.FILES_DIR / user_id / f"{db.new_id('dl_')}.{doc_type}"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if doc_type == "docx":
            report = docx_builder.build_docx(structure, out_path)
            mime = DOCX_MIME
        else:
            from ... import pptx_builder

            report = pptx_builder.build_pptx(structure, out_path)
            mime = PPTX_MIME
        return out_path, report, mime

    out_path, report, mime = await asyncio.to_thread(_build)

    file_row = register_deliverable(
        user_id, task_id, out_path,
        f"{structure.get('title') or '未命名文稿'}.{doc_type}", mime, report,
    )
    return {
        "file_id": file_row["id"],
        "name": file_row["name"],
        "doc_type": doc_type,
        "report": report,
        "text": f"已生成 {file_row['name']}，可在消息流的交付卡片里下载。",
    }


def _plan_prompt(doc_type: str, goal: str) -> str:
    if doc_type == "pptx":
        return (
            "你是演示文稿结构规划助手。只输出 JSON，不要任何解释文字。\n"
            "输出结构：{\"title\":\"标题\",\"subtitle\":\"副标题\","
            "\"slides\":[{\"layout\":\"cover|section|bullets|two_col|quote|chart|end\","
            "\"title\":\"页标题\",\"bullets\":[\"要点1\",\"要点2\"],\"notes\":\"演讲备注\"}]}\n"
            "规则：第 1 页 layout 必须是 cover，最后一页是 end；"
            "中间页至少 2 条 bullets，每条不超过 40 字；slides 至少 5 页。\n\n"
            f"用户目标：{goal}"
        )
    return (
        "你是文档结构规划助手。只输出 JSON，不要任何解释文字。\n"
        "输出结构：{\"title\":\"标题\",\"thesis\":\"一句话主旨\","
        "\"sections\":[{\"heading\":\"小节标题\",\"paras\":[\"第一段\",\"第二段\"]}]}\n"
        "规则：sections 至少 3 个；每个小节 paras 至少 3 段，每段不少于 120 字。\n\n"
        f"用户目标：{goal}"
    )


async def _plan_structure(doc_type: str, goal: str) -> dict:
    """让模型先把目标变成可排版的结构。"""
    system = _plan_prompt(doc_type, goal)
    try:
        result = await llm.complete(
            system,
            [{"role": "user", "content": [{"type": "text", "text": goal}]}],
            model=config.MODEL,
        )
    except llm.UpstreamError as exc:
        raise ToolError(f"生成文档结构失败：{exc.message}") from exc
    raw = (result.get("text") or "").strip()
    try:
        data = llm.extract_json(raw)
    except ValueError as exc:
        raise ToolError(f"模型没有返回可解析的文档结构：{exc}") from exc
    if not isinstance(data, dict):
        raise ToolError("模型没有返回可解析的文档结构")

    if doc_type == "pptx":
        slides = data.get("slides") or []
        if not isinstance(slides, list) or not slides:
            raise ToolError("演示结构里没有幻灯片")
        out_slides = []
        for index, item in enumerate(slides, start=1):
            if not isinstance(item, dict):
                continue
            layout = str(item.get("layout") or ("cover" if index == 1 else "bullets"))
            bullets = item.get("bullets") or []
            if isinstance(bullets, str):
                bullets = [bullets]
            out_slides.append({
                "layout": layout,
                "title": str(item.get("title") or "")[:80],
                "subtitle": str(item.get("subtitle") or "")[:120],
                "bullets": [str(b)[:200] for b in bullets][:8],
                "quote": str(item.get("quote") or "")[:400],
                "notes": str(item.get("notes") or "")[:800],
            })
        if len(out_slides) < 2:
            raise ToolError("演示结构里幻灯片太少")
        return {"title": str(data.get("title") or "未命名演示")[:120],
                "subtitle": str(data.get("subtitle") or "")[:200],
                "slides": out_slides}

    sections = data.get("sections") or []
    if not isinstance(sections, list) or not sections:
        raise ToolError("文档结构里没有小节")
    out_sections = []
    for index, item in enumerate(sections, start=1):
        if not isinstance(item, dict):
            continue
        paras = item.get("paras") or item.get("body") or []
        if isinstance(paras, str):
            paras = [p for p in paras.split("\n") if p.strip()]
        if not isinstance(paras, list):
            paras = [str(paras)]
        out_sections.append({
            "heading": str(item.get("heading") or f"第{index}节")[:80],
            "paras": [str(p)[:4000] for p in paras if str(p).strip()],
        })
    if not out_sections:
        raise ToolError("文档结构里没有有效小节")
    return {
        "title": str(data.get("title") or "未命名文稿")[:120],
        "thesis": str(data.get("thesis") or "")[:400],
        "sections": out_sections,
    }
