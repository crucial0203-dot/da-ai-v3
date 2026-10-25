"""切块。

按段落切，太长的段落再按句号切；相邻块保留重叠（默认 80 字），
避免一句话被切成两半后两边都读不懂。

中文没有空格，不能按空格分词——按标点断句是最省事也最稳的做法。
"""
from __future__ import annotations

import re

_HEADING = re.compile(r"^#{1,6}\s*(.+)$", re.M)
# 句末标点（中英文）
_SENT_END = re.compile(r"(?<=[。！？；!?;])\s*")


def extract_heading(text: str) -> str:
    """取第一个 Markdown 标题作为块的标题线索。"""
    match = _HEADING.search(text or "")
    if match:
        return match.group(1).strip()[:120]
    # 没有标题就用第一行非空文本
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    return ""


def _is_markdown_heading(block: str) -> bool:
    """判断一个块是不是 Markdown 标题（# 开头，可带内容行）。

    必须是 `#` 开头且行数少——普通短段落不是标题。
    """
    lines = [l for l in block.splitlines() if l.strip()]
    if not lines:
        return False
    first = lines[0].strip()
    if not first.startswith("#"):
        return False
    return len(lines) <= 2


def _markdown_heading(block: str) -> str:
    match = _HEADING.match(block.strip())
    if match:
        return match.group(1).strip()[:120]
    return block.strip().splitlines()[0][:120]


def _split_long(text: str, limit: int) -> list[str]:
    """超长段落按句号切；切不动（无标点的长串）就硬切。"""
    if len(text) <= limit:
        return [text]
    parts = [p for p in _SENT_END.split(text) if p]
    out: list[str] = []
    buf = ""
    for part in parts:
        if not buf:
            buf = part
        elif len(buf) + len(part) <= limit:
            buf += part
        else:
            out.append(buf)
            buf = part
    if buf:
        out.append(buf)
    # 仍然超长（无标点）就硬切
    final: list[str] = []
    for part in out:
        while len(part) > limit:
            final.append(part[:limit])
            part = part[limit:]
        if part:
            final.append(part)
    return final


def chunk_text(text: str, chunk_chars: int = 600, overlap: int = 80) -> list[dict]:
    """把长文本切成带 heading 的块列表。

    返回 [{"seq": i, "heading": str, "body": str}, ...]
    """
    text = (text or "").strip()
    if not text:
        return []

    # 先按空行分段（Markdown 段落边界），再按标题分节。
    #
    # 只有 Markdown 标题（# 开头）才算分节标记——普通短段落不能当标题，
    # 否则整段正文会被当成标题丢掉（这是实测踩到的坑）。
    sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    buffer: list[str] = []

    for block in re.split(r"\n{2,}", text):
        block = block.strip()
        if not block:
            continue
        if _is_markdown_heading(block):
            if buffer:
                sections.append((current_heading, buffer))
                buffer = []
            current_heading = _markdown_heading(block)
            continue
        buffer.append(block)
    if buffer:
        sections.append((current_heading, buffer))

    # 全文没有任何 Markdown 标题时，退化成单节（heading 取首行）
    if not sections and text:
        sections = [(extract_heading(text), [text])]

    chunks: list[dict] = []
    seq = 0
    for heading, blocks in sections:
        body = "\n\n".join(blocks)
        for piece in _split_long(body, chunk_chars):
            piece = piece.strip()
            if not piece:
                continue
            chunks.append({"seq": seq, "heading": heading, "body": piece})
            seq += 1

    # 补重叠：把上一块末尾 overlap 字接到下一块开头
    if overlap > 0 and len(chunks) > 1:
        merged: list[dict] = [chunks[0]]
        for current in chunks[1:]:
            tail = merged[-1]["body"][-overlap:]
            merged.append({
                "seq": current["seq"],
                "heading": current["heading"],
                "body": tail + "\n" + current["body"],
            })
        chunks = merged

    # 重排序号
    for index, item in enumerate(chunks):
        item["seq"] = index
    return chunks
