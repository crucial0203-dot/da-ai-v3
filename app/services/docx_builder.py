"""结构 JSON → DOCX，并在生成后回读断言。

原则：模型只给内容，排版全部由代码决定。字体、字号、行距、首行缩进、
页码域、目录域都在这里写死，验证时再读回来核对，不靠"看起来像"。
"""
from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

DEFAULT_FORMAT = {
    "body_font": "宋体",
    "heading_font": "黑体",
    "body_size_pt": 10.5,
    "heading_size_pt": 14,
    "title_size_pt": 18,
    "line_spacing_pt": 22,
    "first_line_indent_pt": 21,
    "margin_cm": 2.54,
    "with_toc": True,
    "with_page_number": True,
}


def count_words(text: str) -> int:
    """中文按字计，英文按词计，混排时相加。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z][A-Za-z'\-]*", text))
    return cjk + latin


def read_sample_format(path: Path) -> dict:
    """从用户上传的格式样张里读出正文字体/字号/行距/缩进。"""
    fmt = dict(DEFAULT_FORMAT)
    try:
        doc = Document(str(path))
        normal = doc.styles["Normal"]
        font = normal.font
        if font.name:
            fmt["body_font"] = font.name
        if font.size:
            fmt["body_size_pt"] = round(font.size.pt, 1)
        # 取第一个正文段落作为行距与缩进依据
        for para in doc.paragraphs:
            if not para.text.strip():
                continue
            spacing = para.paragraph_format.line_spacing
            if spacing is not None and hasattr(spacing, "pt"):
                fmt["line_spacing_pt"] = round(spacing.pt, 1)
            indent = para.paragraph_format.first_line_indent
            if indent is not None:
                fmt["first_line_indent_pt"] = round(indent.pt, 1)
            break
    except Exception:
        pass
    return fmt


def merge_format(overrides: dict | None) -> dict:
    fmt = dict(DEFAULT_FORMAT)
    for key, value in (overrides or {}).items():
        if key in fmt and value not in (None, ""):
            fmt[key] = value
    return fmt


def _set_run_font(run, font_name: str, size_pt: float, bold: bool = False) -> None:
    run.font.name = font_name
    run.font.size = Pt(size_pt)
    run.font.bold = bold
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:ascii"), font_name)
    rfonts.set(qn("w:hAnsi"), font_name)
    rfonts.set(qn("w:eastAsia"), font_name)


def _add_field(paragraph, instr: str, placeholder: str = "") -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr_el = OxmlElement("w:instrText")
    instr_el.set(qn("xml:space"), "preserve")
    instr_el.text = instr
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text_el = OxmlElement("w:t")
    text_el.text = placeholder
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for el in (begin, instr_el, separate, text_el, end):
        run._r.append(el)


def build_docx(structure: dict, out_path: Path, overrides: dict | None = None) -> dict:
    fmt = merge_format(overrides)
    doc = Document()

    section = doc.sections[0]
    section.top_margin = Cm(fmt["margin_cm"])
    section.bottom_margin = Cm(fmt["margin_cm"])
    section.left_margin = Cm(fmt["margin_cm"])
    section.right_margin = Cm(fmt["margin_cm"])

    normal = doc.styles["Normal"]
    normal.font.name = fmt["body_font"]
    normal.font.size = Pt(fmt["body_size_pt"])
    normal_ppr = normal.element.get_or_add_pPr()
    spacing = OxmlElement("w:spacing")
    spacing.set(qn("w:line"), str(int(fmt["line_spacing_pt"] * 20)))
    spacing.set(qn("w:lineRule"), "exact")
    normal_ppr.append(spacing)

    title = (structure.get("title") or "未命名文稿").strip()
    title_para = doc.add_paragraph()
    title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title_para.add_run(title)
    _set_run_font(title_run, fmt["heading_font"], fmt["title_size_pt"], bold=True)

    subtitle = (structure.get("thesis") or "").strip()
    if subtitle:
        sub_para = doc.add_paragraph()
        sub_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sub_run = sub_para.add_run(subtitle)
        _set_run_font(sub_run, fmt["body_font"], fmt["body_size_pt"])

    if fmt.get("with_toc"):
        toc_para = doc.add_paragraph()
        toc_run = toc_para.add_run("目录")
        _set_run_font(toc_run, fmt["heading_font"], fmt["heading_size_pt"], bold=True)
        toc_line = doc.add_paragraph()
        _add_field(toc_line, 'TOC \\o "1-3" \\h \\z \\u', "（在 Word 中按 F9 更新目录）")

    for section_data in structure.get("sections") or []:
        heading = (section_data.get("heading") or "").strip()
        if heading:
            h_para = doc.add_paragraph(style="Heading 1")
            h_para.paragraph_format.space_before = Pt(12)
            h_para.paragraph_format.space_after = Pt(6)
            h_run = h_para.add_run(heading)
            _set_run_font(h_run, fmt["heading_font"], fmt["heading_size_pt"], bold=True)

        paragraphs = section_data.get("paras") or []
        if isinstance(paragraphs, str):
            paragraphs = [p for p in paragraphs.split("\n") if p.strip()]
        for text in paragraphs:
            body = (text or "").strip()
            if not body:
                continue
            para = doc.add_paragraph()
            para.paragraph_format.line_spacing = Pt(fmt["line_spacing_pt"])
            para.paragraph_format.first_line_indent = Pt(fmt["first_line_indent_pt"])
            para.paragraph_format.space_after = Pt(0)
            body_run = para.add_run(body)
            _set_run_font(body_run, fmt["body_font"], fmt["body_size_pt"])

    if fmt.get("with_page_number"):
        footer = doc.sections[0].footer.paragraphs[0]
        footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _add_field(footer, " PAGE ", "1")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.core_properties.title = title
    doc.save(str(out_path))
    return fmt


def verify_docx(path: Path, fmt: dict, expected_words: int, section_count: int) -> dict:
    checks: list[dict] = []
    doc = Document(str(path))
    paragraphs = [p for p in doc.paragraphs if p.text.strip()]

    # 只取第一个标题之后的正文段落：标题之前是题名、副标题与目录，格式本就不同
    first_heading = next(
        (i for i, p in enumerate(paragraphs) if p.style.name.startswith("Heading")), 0
    )
    body_paras = [
        p for p in paragraphs[first_heading:]
        if p.style.name == "Normal" and count_words(p.text) > 20
    ]

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    check("段落非空", len(paragraphs) >= max(3, section_count), f"共 {len(paragraphs)} 段")
    check("章节标题数", sum(1 for p in paragraphs if p.style.name.startswith("Heading")) >= section_count,
          f"标题 {sum(1 for p in paragraphs if p.style.name.startswith('Heading'))} / 期望 {section_count}")

    if body_paras:
        sample = body_paras[0]
        run = sample.runs[0] if sample.runs else None
        font_name = run.font.name if run else None
        size_pt = round(run.font.size.pt, 1) if run and run.font.size else None
        indent_pt = round(sample.paragraph_format.first_line_indent.pt, 1) if sample.paragraph_format.first_line_indent else None
        spacing = sample.paragraph_format.line_spacing
        spacing_pt = round(spacing.pt, 1) if hasattr(spacing, "pt") else None

        check("正文字体", font_name == fmt["body_font"], f"{font_name} / 期望 {fmt['body_font']}")
        check("字号", size_pt == round(float(fmt["body_size_pt"]), 1), f"{size_pt}pt / 期望 {fmt['body_size_pt']}pt")
        check("行距", spacing_pt == round(float(fmt["line_spacing_pt"]), 1), f"{spacing_pt}pt / 期望 {fmt['line_spacing_pt']}pt")
        check("首行缩进", indent_pt == round(float(fmt["first_line_indent_pt"]), 1), f"{indent_pt}pt / 期望 {fmt['first_line_indent_pt']}pt")
    else:
        check("正文段落", False, "没有找到长度超过 20 字的正文段落")

    total_words = count_words("\n".join(p.text for p in paragraphs))
    if expected_words:
        ratio = total_words / expected_words
        check("字数达标", ratio >= 0.7, f"{total_words} 字 / 目标 {expected_words} 字（{ratio:.0%}）")
    check("全文完整", "..." not in "".join(p.text for p in paragraphs)[-50:], "结尾无截断标记")

    footer_xml = doc.sections[0].footer.paragraphs[0]._p.xml if doc.sections[0].footer.paragraphs else ""
    check("页码域", "PAGE" in footer_xml, "页脚含 PAGE 域" if "PAGE" in footer_xml else "页脚没有页码域")

    passed = sum(1 for c in checks if c["ok"])
    return {
        "kind": "docx",
        "ok": passed == len(checks),
        "passed": passed,
        "total": len(checks),
        "checks": checks,
        "stats": {"paragraphs": len(paragraphs), "words": total_words},
    }
