"""任务级材料全文（与 RAG 并存）。

上传时已由 extract.py 抽好文字（落 sidecar txt），这里只负责把「这个任务的
材料正文」按上限拼成一段可直接进 system prompt 的全文。

两种取材方式并存，语义不同：
- 任务级全文（本模块）——写作类任务要把整份材料铺进上下文当依据；
- RAG 切片检索（rag/）——按查询召回 top_k，适合大语料与知识库复用。
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import config, db
from . import prompts

IMAGE_EXT = config.IMAGE_EXT


def materials_text(task_id: str, limit: int = 150_000,
                   exclude_ids: set[str] | None = None) -> tuple[str, list[dict]]:
    """返回 (拼接正文, 材料摘要清单)。exclude_ids 用于排除模板文件（格式资产）。"""
    rows = db.rows(
        "SELECT * FROM files WHERE task_id = ? AND kind = 'source' ORDER BY created_at ASC", (task_id,)
    )
    skip = exclude_ids or set()
    blocks: list[str] = []
    summaries: list[dict] = []
    used = 0
    for row in rows:
        if row["id"] in skip:
            # 模板文件是格式资产，不是材料：它没有正文，不能拿去当依据，
            # 也不能因为"没有可用文字"把整条任务判失败。
            continue
        meta = json.loads(row["meta_json"] or "{}")
        if meta.get("image") or Path(row["path"]).suffix.lower() in IMAGE_EXT:
            # 图片没有文字层，但它也不是"坏材料"。单独归类，别让它把
            # 「有没有可用文字」这个判定污染掉（否则只传图的任务会被报成扫描件）。
            summaries.append({
                "name": row["name"],
                "chars": 0,
                "image": True,
                "note": meta.get("note") or "图片材料，未参与文字抽取",
            })
            continue
        sidecar = config.DATA_DIR / "extracted" / f"{row['id']}.txt"
        text = ""
        if sidecar.exists():
            text = sidecar.read_text(encoding="utf-8", errors="replace")
        if not text:
            summaries.append({"name": row["name"], "chars": 0, "note": meta.get("note") or "无文本"})
            continue
        chunk = text[: max(0, limit - used)]
        used += len(chunk)
        blocks.append(prompts.material_block(row["name"], meta.get("page_count") or "?", chunk))
        summaries.append({
            "name": row["name"],
            "chars": meta.get("chars", len(chunk)),
            "pages": meta.get("page_count"),
            "note": meta.get("note") or "",
        })
        if used >= limit:
            break
    return "\n\n".join(blocks), summaries
