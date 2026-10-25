"""检索。

两条路，同一个接口：
- **FTS5**：SQLite 自带全文索引，`bm25()` 打分，`snippet()` 取引文。
  命中质量更好（会做词干与短语处理），但不是所有编译都带。
  查询词先做字符串字面量转义（内部引号翻倍），否则带引号的查询会语法错误。
- **BM25 兜底**：FTS5 不可用时，SQL 侧按查询词 LIKE 粗筛（≤2000 块，
  按文档名+序号有序取样），再用 Python 实现同款 BM25 打分。
  结果不如 FTS5 精确，但**不返回空**。

安全：检索永远按 collection_id + user_id 过滤，跨用户不可能命中。
"""
from __future__ import annotations

import math
import re
from collections import Counter

from ... import config, db

_TOKEN = re.compile(r"[a-zA-Z0-9_]+|[一-鿿]")


def _tokenize(text: str) -> list[str]:
    """中英混排分词：英文按词、中文按字。

    中文按字会让「行政协议」变成 4 个 token，BM25 仍然能工作
    （相邻字会同时命中，IDF 高的罕见字起到区分作用）。
    """
    text = (text or "").lower()
    tokens = _TOKEN.findall(text)
    out: list[str] = []
    for token in tokens:
        if len(token) >= 2 and token.isascii():
            out.append(token)
        else:
            out.extend(list(token))
    return out


def _bm25(query_tokens: list[str], docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> list[float]:
    """标准 BM25 打分。"""
    if not docs or not query_tokens:
        return [0.0] * len(docs)
    n = len(docs)
    avg_len = sum(len(d) for d in docs) / n or 1.0
    doc_freq: Counter = Counter()
    for doc in docs:
        for token in set(doc):
            doc_freq[token] += 1
    scores = []
    for doc in docs:
        freq = Counter(doc)
        length = len(doc) or 1
        score = 0.0
        for token in query_tokens:
            tf = freq.get(token, 0)
            if not tf:
                continue
            idf = math.log(1 + (n - doc_freq.get(token, 0) + 0.5) / (doc_freq.get(token, 0) + 0.5))
            score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * length / avg_len))
        scores.append(score)
    return scores


def _fts_quote(token: str) -> str:
    """FTS5 字符串字面量转义：内部双引号要写成两个双引号。

    不转义时，查询里带 `"` 的词条会让 MATCH 直接语法错误——被调用方的
    except 吞掉后表现成「检索莫名返回空」，且没有任何日志
    （移植 dify `escape_query_for_search` 的同款防护）。
    """
    return '"' + token.replace('"', '""') + '"'


def _like_escape(token: str) -> str:
    """LIKE 通配符转义（配合 ESCAPE '\\'）：分词可能带下划线，那是通配符不是字面量。"""
    return (
        token.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _fts_search(collection_ids: list[str], query: str, top_k: int) -> list[dict]:
    """FTS5 路径。"""
    if not collection_ids:
        return []
    marks = ",".join("?" * len(collection_ids))
    sql = (
        "SELECT c.id AS chunk_id, c.doc_id, c.seq, c.heading, c.body, c.collection_id, "
        "       d.name AS doc_name, "
        "       bm25(rag_chunks_fts, 1.0, 0.5) AS score, "
        "       snippet(rag_chunks_fts, 0, '<<<', '>>>', ' … ', 24) AS snip "
        f"FROM rag_chunks_fts JOIN rag_chunks c ON c.id = rag_chunks_fts.rowid "
        "JOIN rag_documents d ON d.id = c.doc_id "
        f"WHERE rag_chunks_fts MATCH ? AND c.collection_id IN ({marks}) "
        "ORDER BY score LIMIT ?"
    )
    # FTS5 的 MATCH 语法：中文要加引号当短语，否则被当成列名或语法错误
    terms = [t for t in _tokenize(query) if t.strip()]
    if not terms:
        return []
    match_expr = " OR ".join(_fts_quote(t) for t in terms[:24])
    params = [match_expr, *collection_ids, top_k * 2]
    try:
        rows = db.rows(sql, params)
    except Exception:
        return []
    return [
        {
            "chunk_id": r["chunk_id"], "doc_id": r["doc_id"], "seq": r["seq"],
            "heading": r["heading"], "body": r["body"],
            "collection_id": r["collection_id"], "doc_name": r["doc_name"],
            "score": -float(r["score"] or 0), "snippet": r["snip"] or "",
        }
        for r in rows
    ]


def _bm25_search(collection_ids: list[str], query: str, top_k: int) -> list[dict]:
    """BM25 兜底路径。"""
    if not collection_ids:
        return []
    marks = ",".join("?" * len(collection_ids))
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []
    # 粗筛下推到 SQL。原实现是 `LIMIT 500` 且不带 ORDER BY：只扫物理上
    # 前 500 块，排在后面的文档永远检索不到，而用户看到的是「没相关内容」。
    # 现在先按 token 做 LIKE 粗筛（至少命中一个查询词），再按 文档名+序号
    # 有序取样；上限从 500 放到 2000，给 BM25 留出有效候选池。
    like_terms = sorted({t for t in query_tokens if t.strip()})[:24]
    if not like_terms:
        return []
    where = " OR ".join(["(c.body LIKE ? ESCAPE '\\' OR c.heading LIKE ? ESCAPE '\\')"] * len(like_terms))
    sql = (
        "SELECT c.id AS chunk_id, c.doc_id, c.seq, c.heading, c.body, c.collection_id, "
        "       d.name AS doc_name FROM rag_chunks c "
        "JOIN rag_documents d ON d.id = c.doc_id "
        f"WHERE c.collection_id IN ({marks}) AND ({where}) "
        "ORDER BY d.name, c.seq LIMIT 2000"
    )
    params: list = list(collection_ids)
    for t in like_terms:
        params.extend([f"%{_like_escape(t)}%", f"%{_like_escape(t)}%"])
    rows = db.rows(sql, params)
    if not rows:
        return []
    # 粗筛：至少含一个查询 token 的块才参与打分（LIKE 已预筛，这里保精确语义）
    qset = set(query_tokens)
    candidates = [r for r in rows if qset & set(_tokenize(r["body"] + " " + (r["heading"] or "")))]
    if not candidates:
        return []
    docs = [_tokenize(r["body"] + " " + (r["heading"] or "")) for r in candidates]
    scores = _bm25(query_tokens, docs)
    scored = []
    for row, score in zip(candidates, scores):
        if score <= 0:
            continue
        scored.append({
            "chunk_id": row["chunk_id"], "doc_id": row["doc_id"], "seq": row["seq"],
            "heading": row["heading"], "body": row["body"],
            "collection_id": row["collection_id"], "doc_name": row["doc_name"],
            "score": score, "snippet": "",
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[: top_k * 2]


def search(collection_ids: list[str], user_id: str, query: str,
           top_k: int | None = None) -> list[dict]:
    """检索。

    返回 [{"n": 1, "doc_name", "heading", "body", "score", "collection_id"}...]
    n 是注入模型时的引用编号。
    """
    top_k = top_k or config.RAG_TOP_K
    collection_ids = [c for c in (collection_ids or []) if c]
    if not collection_ids or not (query or "").strip():
        return []

    # 用户可见性：集合必须属于该用户
    marks = ",".join("?" * len(collection_ids))
    visible = db.rows(
        f"SELECT id FROM rag_collections WHERE user_id = ? AND id IN ({marks})",
        [user_id, *collection_ids],
    )
    allowed = [r["id"] for r in visible]
    if not allowed:
        return []

    hits: list[dict] = []
    if db.fts5_enabled():
        hits = _fts_search(allowed, query, top_k)
    if not hits:
        hits = _bm25_search(allowed, query, top_k)

    out: list[dict] = []
    for index, hit in enumerate(hits[:top_k]):
        body = (hit["body"] or "")[: config.RAG_MAX_CHUNK_CHARS]
        out.append({
            "n": index + 1,
            "doc_name": hit.get("doc_name") or "",
            "heading": hit.get("heading") or "",
            "body": body,
            "score": round(float(hit.get("score") or 0), 4),
            "collection_id": hit.get("collection_id") or "",
            "doc_id": hit.get("doc_id") or "",
        })
    return out


def format_context(hits: list[dict]) -> str:
    """把命中格式化成注入块，带 [n] 编号。"""
    if not hits:
        return ""
    parts = ["<<<RAG>>>"]
    for hit in hits:
        head = hit.get("heading") or ""
        name = hit.get("doc_name") or ""
        title = " · ".join(x for x in (name, head) if x)
        parts.append(f"[{hit['n']}] {title}\n{hit['body']}".strip())
    parts.append("<<<END_RAG>>>")
    return "\n\n".join(parts)
