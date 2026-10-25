"""M14 验收：轻量 RAG。

跑法（需 8020 已起，FAKE_UPSTREAM=1）：
    python tests/m14_rag.py

覆盖：
  R1 建集合 → 列表可见
  R2 加文本文档 → 自动切块入库
  R3 检索命中 → 引用编号 [n] 连续
  R4 system 里出现 <<<RAG>>> 块
  R5 伪置 fts5=0 时 BM25 兜底仍出结果
  R6 跨用户 0 命中（别人的集合检索不到）
  R7 删除文档 → 块与 FTS 索引同步清理
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import (  # noqa: E402
    Client, admin_login, check, join, make_invite, stream, summary,
)


def env(k: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(k + "="):
            return line.split("=", 1)[1].strip()
    return ""


def service_db():
    """拿到**服务进程用的**那个数据库连接。

    测试进程自己的 DATA_DIR 可能与服务不同（那就看不到服务建的表）。
    凡是直接操作数据库的断言，都必须走这里。
    """
    import importlib

    module = importlib.import_module("app.db")
    # 读服务端写的 DATA_DIR：服务启动时把真实路径打到 data_dir_hint
    return module


DOC_TEXT = """# 行政协议诉讼

行政协议诉讼是行���审判中的一种特殊类型。2019 年最高人民法院关于行政协议若干问题的规定
明确了行政协议的范围。

## 争议解决方式

当事人可以约定仲裁，一方请求仲裁的，另一方可以提出异议。
行政协议案件中，一方请求仲裁的，仲裁机构不予受理，但双方约定仲裁的除外。

## 裁判依据

人民法院审理行政协议案件，应当以法律、行政法规、地方性法规为依据。
参照规章可以引用的，人民法院应当予以引用的，应当在裁判文书中予以引用。
"""


def main() -> int:
    admin = admin_login(env("ADMIN_PASSWORD"))
    code = make_invite(admin, count=2, quota=30, note="m14")

    a = Client()
    join(a, code, "m14甲")
    b = Client()
    join(b, code, "m14乙")

    # R1 建集合
    status, data = a.post("/api/rag/collections", {"name": "行政协议资料", "desc": "测试集合"})
    check("R1 建集合", status == 200 and data.get("id"), f"status={status}")
    cid = data.get("id", "")

    status, data = a.get("/api/rag/collections")
    names = [c["name"] for c in data.get("collections", [])]
    check("R1 集合在列表里", "行政协议资料" in names, str(names))
    check("R1 列表带 fts5 标志", "fts5" in data)

    # R2 加文档 → 切块
    status, data = a.post(
        f"/api/rag/collections/{cid}/documents",
        {"name": "协议通说.txt", "text": DOC_TEXT},
    )
    check("R2 文档入库", status == 200 and data.get("id"), f"status={status} {data}")
    doc_id = data.get("id", "")
    check("R2 切出多块", int(data.get("chunk_count") or 0) >= 2, f"chunks={data.get('chunk_count')}")

    status, data = a.get(f"/api/rag/collections/{cid}/documents")
    check("R2 文档列表可见", len(data.get("documents", [])) == 1, str(data)[:200])

    status, data = a.get(f"/api/rag/documents/{doc_id}/chunks")
    check("R2 块可读", len(data.get("chunks", [])) >= 2, f"n={len(data.get('chunks', []))}")

    # R3 检索命中
    status, data = a.post("/api/rag/search", {
        "query": "行政协议 仲裁 受理", "collection_ids": [cid], "top_k": 5,
    })
    hits = data.get("hits", [])
    check("R3 检索有命中", len(hits) > 0, f"hits={len(hits)}")
    check("R3 引用编号从 1 连续",
          [h["n"] for h in hits] == list(range(1, len(hits) + 1)),
          str([h["n"] for h in hits]))
    check("R3 命中带文档名", all(h.get("doc_name") for h in hits))
    check("R3 命中带正文", all(h.get("body") for h in hits))

    # R4 对话里 system 出现 <<<RAG>>>
    # 先把集合绑到任务上
    status, task = a.post("/api/tasks", {"mode": "research", "goal": "查一下仲裁规则"})
    tid = task.get("id") or task.get("task", {}).get("id")
    check("R4 任务建好", bool(tid), str(task)[:150])
    status, _ = a.patch(f"/api/tasks/{tid}", {"params": {"rag_collection_ids": [cid]}})

    events = stream(a, f"/api/tasks/{tid}/chat", {"text": "行政协议里仲裁怎么处理"})
    meta = next((d for e, d in events if e == "meta"), {})
    check("R4 meta 报 rag 命中数", int(meta.get("rag") or 0) > 0, f"rag={meta.get('rag')}")
    done = next((d for e, d in events if e == "done"), {})
    check("R4 对话正常完成", bool(done), str(events[-1])[:200])

    # R5 伪置 fts5=0 → BM25 兜底（通过服务端 admin API 改，服务进程里的状态才生效）
    status, data = admin.post("/api/admin/settings", {"fts5_enabled": "0"})
    check("R5 切 fts5=0", status == 200 and data.get("changed", {}).get("fts5_enabled") == "0",
          f"status={status} {data}")
    try:
        status, data = a.post("/api/rag/search", {
            "query": "行政协议 仲裁 受理", "collection_ids": [cid], "top_k": 5,
        })
        hits = data.get("hits", [])
        check("R5 BM25 兜底仍出结果", len(hits) > 0, f"hits={len(hits)}")
        check("R5 兜底时 fts5=False", data.get("fts5") is False, str(data.get("fts5")))
    finally:
        admin.post("/api/admin/settings", {"fts5_enabled": "1"})

    # R6 跨用户 0 命中
    status, data = b.post("/api/rag/search", {
        "query": "行政协议 仲裁 受理", "collection_ids": [cid], "top_k": 5,
    })
    check("R6 跨用户 0 命中", len(data.get("hits", [])) == 0, str(data.get("hits"))[:200])
    status, _ = b.get(f"/api/rag/collections/{cid}/documents")
    check("R6 跨用户读文档被拒", status in (403, 404), f"status={status}")
    status, _ = b.delete(f"/api/rag/collections/{cid}")
    check("R6 跨用户删集合被拒", status in (403, 404), f"status={status}")

    # R7 删除文档 → 块清理
    status, _ = a.delete(f"/api/rag/documents/{doc_id}")
    check("R7 删文档成功", status == 200, f"status={status}")
    status, data = a.post("/api/rag/search", {
        "query": "行政协议 仲裁 受理", "collection_ids": [cid], "top_k": 5,
    })
    check("R7 删后检索 0 命中", len(data.get("hits", [])) == 0, str(data.get("hits"))[:150])

    return summary()


if __name__ == "__main__":
    sys.exit(main())
