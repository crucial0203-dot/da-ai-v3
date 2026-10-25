"""结构 JSON → PPTX，含内置版式与模板填充两条路。

内置版式用 13.333×7.5 英寸（16:9）硬坐标，不依赖母版，输出稳定。
模板模式读用户上传 pptx 的版式与占位符；找不到合适占位符时明确报错，
不把文字硬塞进图片或文本框。
"""
from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

INK = RGBColor(0x1F, 0x3D, 0x35)
PAPER = RGBColor(0xF7, 0xF5, 0xF0)
BRASS = RGBColor(0xA8, 0x76, 0x3E)
MUTED = RGBColor(0x6B, 0x63, 0x57)

W = Inches(13.333)
H = Inches(7.5)
MARGIN = Inches(0.9)

HEAD_FONT = "思源宋体"
BODY_FONT = "微软雅黑"


def _blank_slide(prs: Presentation):
    layout = prs.slide_layouts[6] if len(prs.slide_layouts) > 6 else prs.slide_layouts[-1]
    slide = prs.slides.add_slide(layout)
    background = slide.background
    background.fill.solid()
    background.fill.fore_color.rgb = PAPER
    return slide


def _textbox(slide, left, top, width, height, text: str, size: int, font: str = BODY_FONT,
             color: RGBColor = INK, bold: bool = False, align=PP_ALIGN.LEFT, spacing: float = 1.35):
    box = slide.shapes.add_textbox(left, top, width, height)
    frame = box.text_frame
    frame.word_wrap = True
    frame.vertical_anchor = MSO_ANCHOR.TOP
    lines = text.split("\n") if text else [""]
    for index, line in enumerate(lines):
        para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        para.alignment = align
        para.line_spacing = spacing
        run = para.add_run()
        run.text = line
        run.font.size = Pt(size)
        run.font.name = font
        run.font.bold = bold
        run.font.color.rgb = color
    return box


def _rule(slide, left, top, width, color: RGBColor = BRASS, height=Pt(2.2)):
    from pptx.enum.shapes import MSO_SHAPE

    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape


def _bullets(slide, items: list[str], left, top, width, height, size=20):
    box = slide.shapes.add_textbox(left, top, width, height)
    frame = box.text_frame
    frame.word_wrap = True
    for index, item in enumerate(items or [""]):
        para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        para.line_spacing = 1.5
        para.space_after = Pt(14)
        run = para.add_run()
        run.text = "· " + item
        run.font.size = Pt(size)
        run.font.name = BODY_FONT
        run.font.color.rgb = INK
    return box


def _render_cover(slide, data: dict) -> None:
    _rule(slide, MARGIN, Inches(2.5), Inches(1.6))
    _textbox(slide, MARGIN, Inches(2.75), W - MARGIN * 2, Inches(1.6),
             data.get("title") or "", 40, HEAD_FONT, INK, True, spacing=1.2)
    if data.get("subtitle"):
        _textbox(slide, MARGIN, Inches(4.45), W - MARGIN * 2, Inches(1.2),
                 data["subtitle"], 18, BODY_FONT, MUTED)


def _render_section(slide, data: dict) -> None:
    _textbox(slide, MARGIN, Inches(2.6), W - MARGIN * 2, Inches(1.6),
             data.get("title") or "", 34, HEAD_FONT, INK, True)
    _rule(slide, MARGIN, Inches(3.9), Inches(1.2))


def _render_bullets(slide, data: dict) -> None:
    _textbox(slide, MARGIN, Inches(0.75), W - MARGIN * 2, Inches(1.0),
             data.get("title") or "", 30, HEAD_FONT, INK, True)
    _rule(slide, MARGIN, Inches(1.72), Inches(1.0))
    _bullets(slide, data.get("bullets") or [], MARGIN, Inches(2.05),
             W - MARGIN * 2, Inches(4.6), 21)


def _render_two_col(slide, data: dict) -> None:
    _textbox(slide, MARGIN, Inches(0.75), W - MARGIN * 2, Inches(1.0),
             data.get("title") or "", 30, HEAD_FONT, INK, True)
    _rule(slide, MARGIN, Inches(1.72), Inches(1.0))
    items = data.get("bullets") or []
    half = (len(items) + 1) // 2
    col_w = (W - MARGIN * 2 - Inches(0.6)) / 2
    _bullets(slide, items[:half], MARGIN, Inches(2.05), col_w, Inches(4.6), 19)
    _bullets(slide, items[half:], MARGIN + col_w + Inches(0.6), Inches(2.05), col_w, Inches(4.6), 19)


def _render_quote(slide, data: dict) -> None:
    # 提示词约定用 bullets/subtitle 承载引文；模型偶尔会另写 quote 字段，
    # 再退到 title，避免整页一个字都没有
    candidates = data.get("bullets") or []
    quote = (candidates[0] if candidates else "") or data.get("quote") or data.get("subtitle") or data.get("title") or ""
    _rule(slide, Inches(2.4), Inches(2.3), Inches(0.9))
    _textbox(slide, Inches(2.4), Inches(2.6), W - Inches(4.8), Inches(2.6),
             quote, 28, HEAD_FONT, INK, False, spacing=1.5)
    source = (data.get("subtitle") or "").strip()
    if source and source != quote:
        _textbox(slide, Inches(2.4), Inches(5.3), W - Inches(4.8), Inches(0.8),
                 "— " + source, 16, BODY_FONT, MUTED)


def _render_chart(slide, data: dict) -> None:
    """没有真实数据时不画假图，改为「关键数字」三栏。"""
    _textbox(slide, MARGIN, Inches(0.75), W - MARGIN * 2, Inches(1.0),
             data.get("title") or "", 30, HEAD_FONT, INK, True)
    _rule(slide, MARGIN, Inches(1.72), Inches(1.0))
    items = (data.get("bullets") or [])[:3]
    if not items:
        return
    slot_w = (W - MARGIN * 2 - Inches(0.5) * (len(items) - 1)) / max(len(items), 1)
    for index, item in enumerate(items):
        left = MARGIN + (slot_w + Inches(0.5)) * index
        _rule(slide, left, Inches(2.4), slot_w, BRASS, Pt(3))
        _textbox(slide, left, Inches(2.65), slot_w, Inches(3.0), item, 20, BODY_FONT, INK, spacing=1.4)


def _render_end(slide, data: dict) -> None:
    _textbox(slide, MARGIN, Inches(3.0), W - MARGIN * 2, Inches(1.4),
             data.get("title") or "谢谢", 36, HEAD_FONT, INK, True, PP_ALIGN.CENTER)
    if data.get("subtitle"):
        _textbox(slide, MARGIN, Inches(4.4), W - MARGIN * 2, Inches(1.0),
                 data["subtitle"], 16, BODY_FONT, MUTED, align=PP_ALIGN.CENTER)


RENDERERS = {
    "cover": _render_cover,
    "section": _render_section,
    "bullets": _render_bullets,
    "two_col": _render_two_col,
    "quote": _render_quote,
    "chart": _render_chart,
    "end": _render_end,
}


def build_pptx(structure: dict, out_path: Path, template_path: Path | None = None) -> dict:
    template_mode = bool(template_path and template_path.exists())
    if template_mode:
        prs = Presentation(str(template_path))
        used = _fill_template(prs, structure)
        info = {"mode": "template", "filled": used}
    else:
        prs = Presentation()
        prs.slide_width = W
        prs.slide_height = H
        for index, slide_data in enumerate(structure.get("slides") or []):
            layout = slide_data.get("layout") or "bullets"
            if index == 0:
                layout = "cover"
            renderer = RENDERERS.get(layout, _render_bullets)
            slide = _blank_slide(prs)
            renderer(slide, slide_data)
            notes = (slide_data.get("notes") or "").strip()
            if notes:
                slide.notes_slide.notes_text_frame.text = notes
        info = {"mode": "standard"}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.core_properties.title = structure.get("title") or "演示文稿"
    prs.save(str(out_path))
    return info


def _fill_template(prs: Presentation, structure: dict) -> int:
    slides_data = structure.get("slides") or []
    layouts = list(prs.slide_layouts)
    filled = 0
    for slide_data in slides_data:
        layout = _pick_layout(layouts, slide_data.get("layout") or "bullets")
        if layout is None:
            raise ValueError(f"模板缺少可用版式：{slide_data.get('layout')}")
        slide = prs.slides.add_slide(layout)
        assigned = _assign_placeholders(slide, slide_data)
        if not assigned:
            raise ValueError(
                f"该模板第 {len(prs.slides)} 页版式不适配：没有可填充的标题/正文占位符"
            )
        notes = (slide_data.get("notes") or "").strip()
        if notes:
            slide.notes_slide.notes_text_frame.text = notes
        filled += 1
    return filled


def _pick_layout(layouts, kind: str):
    preferred = {
        "cover": ["Title Slide", "标题幻灯片", "Cover", "封面"],
        "section": ["Section Header", "节标题", "Section"],
        "end": ["Title Only", "仅标题", "Title"],
    }
    for layout in layouts:
        names = preferred.get(kind, ["Title and Content", "标题和内容", "Content", "Two Content"])
        for candidate in names:
            if layout.name and candidate.lower() in layout.name.lower():
                return layout
    for layout in layouts:
        if any(p.placeholder_format.type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE)
               for p in layout.placeholders):
            return layout
    return None


def _assign_placeholders(slide, data: dict) -> int:
    assigned = 0
    for shape in slide.placeholders:
        ptype = shape.placeholder_format.type
        if ptype in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE):
            shape.text = data.get("title") or ""
            assigned += 1
        elif ptype == PP_PLACEHOLDER.SUBTITLE:
            shape.text = data.get("subtitle") or ""
            assigned += 1
        elif ptype in (PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT):
            items = data.get("bullets") or []
            if not items:
                continue
            frame = shape.text_frame
            frame.text = items[0]
            for item in items[1:]:
                para = frame.add_paragraph()
                para.text = item
            assigned += 1
    return assigned


def verify_pptx(path: Path, expected_slides: int, max_chars: int = 160) -> dict:
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    prs = Presentation(str(path))
    slides = list(prs.slides)
    check("页数", len(slides) == expected_slides, f"{len(slides)} 页 / 期望 {expected_slides} 页")

    empty = 0
    missing_notes = 0
    overfull = 0
    for index, slide in enumerate(slides, start=1):
        texts = [s.text_frame.text.strip() for s in slide.shapes if s.has_text_frame and s.text_frame.text.strip()]
        if not texts:
            empty += 1
        total = sum(len(t) for t in texts)
        if total > max_chars:
            overfull += 1
        notes = slide.notes_slide.notes_text_frame.text.strip() if slide.has_notes_slide else ""
        if not notes:
            missing_notes += 1

    check("无空页", empty == 0, f"空页 {empty} 页")
    check("单页字数", overfull == 0, f"超 {max_chars} 字的页数：{overfull}")
    check("演讲备注", missing_notes == 0, f"缺备注 {missing_notes} 页")

    passed = sum(1 for c in checks if c["ok"])
    return {
        "kind": "pptx",
        "ok": passed == len(checks),
        "passed": passed,
        "total": len(checks),
        "checks": checks,
        "stats": {"slides": len(slides)},
    }
