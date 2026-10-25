"""V4.0 N4 知识库路由：集合、文档、检索。

权限模型：集合属于某个用户，别人拿不到（检索层再按 user_id 过滤一次，
不靠路由层守——两道门都留着）。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import config, db, deps
from ..services import rag

router = APIRouter(prefix="/api/rag", tags=["rag"])


class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    desc: str = Field(default="", max_length=300)


class QueryIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    collection_ids: list[str] = Field(default_factory=list)
    top_k: int = Field(default=5, ge=1, le=20)


def _own_collection(collection_id: str, user_id: str) -> dict:
    row = db.one("SELECT * FROM rag_collections WHERE id = ?", (collection_id,))
    if not row:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if row["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="无权访问这个知识库")
    return dict(row)


@router.get("/collections")
def list_collections(request: Request):
    user = deps.current_user(request)
    rows = db.rows(
        "SELECT c.id, c.name, c.desc, c.created_at, "
        "(SELECT COUNT(*) FROM rag_documents d WHERE d.collection_id = c.id) AS doc_count, "
        "(SELECT COALESCE(SUM(d.chunk_count), 0) FROM rag_documents d WHERE d.collection_id = c.id) AS chunk_count "
        "FROM rag_collections c WHERE c.user_id = ? ORDER BY c.created_at DESC",
        (user["id"],),
    )
    return {"collections": [dict(r) for r in rows], "fts5": db.fts5_enabled()}


@router.post("/collections")
def create_collection(payload: CollectionIn, request: Request):
    user = deps.current_user(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="知识库名称不能为空")
    existing = db.one(
        "SELECT id FROM rag_collections WHERE user_id = ? AND name = ?", (user["id"], name)
    )
    if existing:
        raise HTTPException(status_code=400, detail=f"已经有叫「{name}」的知识库了")
    cid = db.new_id("rc_")
    db.execute(
        "INSERT INTO rag_collections(id, user_id, name, desc, created_at) VALUES(?,?,?,?,?)",
        (cid, user["id"], name, payload.desc.strip(), db.now_iso()),
    )
    return {"ok": True, "id": cid, "name": name}


@router.delete("/collections/{collection_id}")
def delete_collection(collection_id: str, request: Request):
    user = deps.current_user(request)
    _own_collection(collection_id, user["id"])
    with db.tx() as conn:
        conn.execute("DELETE FROM rag_chunks WHERE collection_id = ?", (collection_id,))
        conn.execute("DELETE FROM rag_documents WHERE collection_id = ?", (collection_id,))
        conn.execute("DELETE FROM rag_collections WHERE id = ?", (collection_id,))
    return {"ok": True}


@router.get("/collections/{collection_id}/documents")
def list_documents(collection_id: str, request: Request):
    user = deps.current_user(request)
    _own_collection(collection_id, user["id"])
    rows = db.rows(
        "SELECT id, file_id, name, chars, chunk_count, status, error, created_at "
        "FROM rag_documents WHERE collection_id = ? ORDER BY created_at DESC",
        (collection_id,),
    )
    return {"documents": [dict(r) for r in rows]}


@router.post("/collections/{collection_id}/documents")
def add_text_document(collection_id: str, payload: dict, request: Request):
    """直接用一段文本建文档（不经过上传）。

    上传文件那条路是 files.py 的事；这里给「粘贴一段文字就进库」的轻入口。
    """
    user = deps.current_user(request)
    _own_collection(collection_id, user["id"])
    name = str(payload.get("name") or "手动添加").strip()[:120]
    text = str(payload.get("text") or "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="文本内容为空")
    if len(text) > config.RAG_MAX_DOC_CHARS:
        raise HTTPException(status_code=413, detail=f"文本超过 {config.RAG_MAX_DOC_CHARS} 字上限")
    doc_id = rag.index_file(collection_id, None, name, text)
    row = db.one("SELECT chunk_count FROM rag_documents WHERE id = ?", (doc_id,))
    return {"ok": True, "id": doc_id, "chunk_count": row["chunk_count"] if row else 0}


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: str, request: Request):
    user = deps.current_user(request)
    row = db.one("SELECT * FROM rag_documents WHERE id = ?", (doc_id,))
    if not row:
        raise HTTPException(status_code=404, detail="文档不存在")
    collection = db.one("SELECT user_id FROM rag_collections WHERE id = ?", (row["collection_id"],))
    if not collection or collection["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="无权删除这个文档")
    rag.delete_document(doc_id)
    return {"ok": True}


@router.post("/search")
def search(payload: QueryIn, request: Request):
    user = deps.current_user(request)
    hits = rag.search(payload.collection_ids, user["id"], payload.query, payload.top_k)
    return {"hits": hits, "fts5": db.fts5_enabled()}


@router.get("/documents/{doc_id}/chunks")
def list_chunks(doc_id: str, request: Request, limit: int = 50):
    user = deps.current_user(request)
    row = db.one("SELECT * FROM rag_documents WHERE id = ?", (doc_id,))
    if not row:
        raise HTTPException(status_code=404, detail="文档不存在")
    collection = db.one("SELECT user_id FROM rag_collections WHERE id = ?", (row["collection_id"],))
    if not collection or collection["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="无权查看这个文档")
    rows = db.rows(
        "SELECT id, seq, heading, substr(body, 1, 400) AS body_head, tokens "
        "FROM rag_chunks WHERE doc_id = ? ORDER BY seq LIMIT ?",
        (doc_id, max(1, min(limit, 200))),
    )
    return {"chunks": [dict(r) for r in rows]}


@router.post("/index-file")
def index_file(payload: dict, request: Request):
    """把已上传的文件索引进某个知识库。"""
    user = deps.current_user(request)
    file_id = str(payload.get("file_id") or "")
    collection_id = str(payload.get("collection_id") or "")
    _own_collection(collection_id, user["id"])
    row = db.one("SELECT * FROM files WHERE id = ?", (file_id,))
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="文件不存在")
    sidecar = config.DATA_DIR / "extracted" / f"{file_id}.txt"
    text = sidecar.read_text(encoding="utf-8", errors="replace") if sidecar.exists() else ""
    if not text.strip():
        raise HTTPException(status_code=400, detail="这个文件没有可索引的文本（扫描版 PDF 或图片需要 OCR）")
    doc_id = rag.index_file(collection_id, file_id, row["name"], text)
    return {"ok": True, "id": doc_id}
