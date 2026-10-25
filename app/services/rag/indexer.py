"""索引：把文本切块写进 rag_chunks。

写入走一个事务，块与文档状态一起提交——半截索引比没索引更糟：
搜出来的东西缺一半，用户会以为资料本身就这样。
"""
from __future__ import annotations

from ... import config, db
from .chunker import chunk_text


def index_text(collection_id: str, doc_id: str, text: str,
                chunk_chars: int | None = None, overlap: int | None = None) -> int:
    """给一个文档建索引，返回块数。"""
    chunk_chars = chunk_chars or config.RAG_CHUNK_CHARS
    overlap = config.RAG_CHUNK_OVERLAP if overlap is None else overlap
    chunks = chunk_text(text, chunk_chars, overlap)
    if not chunks:
        return 0
    now = db.now_iso()
    with db.tx() as conn:
        conn.execute("DELETE FROM rag_chunks WHERE doc_id = ?", (doc_id,))
        for item in chunks:
            conn.execute(
                "INSERT INTO rag_chunks(doc_id, collection_id, seq, heading, body, tokens, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (doc_id, collection_id, item["seq"], item["heading"], item["body"],
                 max(1, len(item["body"]) // 2), now),
            )
        conn.execute(
            "UPDATE rag_documents SET chunk_count = ?, chars = ?, status = 'ready' WHERE id = ?",
            (len(chunks), len(text or ""), doc_id),
        )
    return len(chunks)


def delete_document(doc_id: str) -> None:
    """删文档连带删块。FTS 触发器会自动同步。"""
    with db.tx() as conn:
        conn.execute("DELETE FROM rag_chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM rag_documents WHERE id = ?", (doc_id,))


def index_file(collection_id: str, file_id: str, name: str, text: str) -> str:
    """给上传的文件建索引（新建文档记录 + 切块）。返回文档 id。"""
    doc_id = db.new_id("rd_")
    db.execute(
        "INSERT INTO rag_documents(id, collection_id, file_id, name, chars, chunk_count, status, created_at) "
        "VALUES(?,?,?,?,?,0,'indexing',?)",
        (doc_id, collection_id, file_id, name, len(text or ""), db.now_iso()),
    )
    try:
        count = index_text(collection_id, doc_id, text)
    except Exception as exc:  # noqa: BLE001
        db.execute(
            "UPDATE rag_documents SET status = 'failed', error = ? WHERE id = ?",
            (f"{type(exc).__name__}: {exc}", doc_id),
        )
        raise
    return doc_id
