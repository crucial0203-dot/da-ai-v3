"""V4.0 N4 轻量 RAG。

流程：上传 → extract → 切块 → 索引 → 检索 → 注入（带 [n] 引用编号）。

**检索层两条路，同一个接口**：
- FTS5（`rag_chunks_fts` 虚表）：有就用，SQLite 自带的全文索引，快；
- BM25 兜底：FTS5 不可用时（沙箱、部分 Linux 发行版缺编译），用 Python 打分。

调用方不关心走的是哪条路——`search()` 永远返回同样结构的命中列表。
"""
from __future__ import annotations

from .chunker import chunk_text, extract_heading
from .indexer import delete_document, index_file, index_text
from .retriever import format_context, search

__all__ = [
    "chunk_text", "extract_heading",
    "index_text", "index_file", "delete_document",
    "search", "format_context",
]
