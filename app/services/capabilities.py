"""Office 能力矩阵：唯一事实源。

规则（不可绕过）：
- `verified=True` 才允许对被调用；否则后端一律 501，前端一律标「暂不支持」。
- 一项能力要从 False 翻成 True，必须先在 `test/` 里有回读脚本并跑过真实样本。
- 界面上的徽标直接来自 `/api/meta`，不另写一份文案，避免两处不一致。
"""
from __future__ import annotations

from fastapi import HTTPException

CAPABILITIES: dict[str, dict] = {
    "docx.create": {
        "label": "新建 Word 文档",
        "verified": True,
        "note": "宋体/黑体、10.5pt、行距 22pt、首行缩进 21pt、目录域、页码域；生成后 9 项回读断言",
    },
    "docx.read_format_sample": {
        "label": "照样张排版",
        "verified": True,
        "note": "只读样张的字体、字号、行距、缩进，不复制内容",
    },
    "docx.edit_existing": {
        "label": "改已有 Word 文件",
        "verified": False,
        "reason": "原地修改涉及修订、批注、内容控件、脚注、公式，尚未做兼容性验收",
    },
    "pptx.create": {
        "label": "新建演示文稿",
        "verified": True,
        "note": "16:9、7 种版式、每页演讲备注；生成后 4 项回读断言",
    },
    "pptx.fill_template": {
        "label": "套用 PPT 模板",
        "verified": True,
        "note": "仅支持带可识别占位符的模板；找不到占位符直接报错，不硬塞文本框",
    },
    "pptx.edit_existing": {
        "label": "改已有 PPT 文件",
        "verified": False,
        "reason": "SmartArt、嵌入图表、母版级改动尚未验证",
    },
    "pdf.extract_text": {
        "label": "按页读 PDF",
        "verified": True,
        "note": "逐页抽取并标注页码；无文字层的扫描件直接报错",
    },
    "pdf.edit": {
        "label": "编辑 PDF",
        "verified": False,
        "reason": "第一期不做 PDF 编辑与 OCR",
    },
    "xlsx.any": {
        "label": "Excel",
        "verified": False,
        "reason": "第一期不支持表格文件",
    },
    "office.render_preview": {
        "label": "渲染预览",
        "verified": False,
        "reason": "沙箱没有 LibreOffice，无法把 DOCX/PPTX 渲染成图片",
    },
}


def is_verified(key: str) -> bool:
    return bool(CAPABILITIES.get(key, {}).get("verified"))


def require(key: str) -> None:
    """不支持的能力直接 501，并带上真实原因，不假装能处理。"""
    item = CAPABILITIES.get(key)
    if not item:
        raise HTTPException(status_code=501, detail="这个能力还没有登记")
    if not item["verified"]:
        raise HTTPException(status_code=501, detail=item.get("reason") or "暂不支持")


def public_matrix() -> list[dict]:
    """给前端渲染徽标用。"""
    return [
        {
            "key": key,
            "label": item["label"],
            "verified": item["verified"],
            "note": item.get("note") or item.get("reason") or "",
        }
        for key, item in CAPABILITIES.items()
    ]
