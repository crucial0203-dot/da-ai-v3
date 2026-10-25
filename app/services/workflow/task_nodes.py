"""任务节点机（task_nodes 表）：文档/PPT 生成的阶段状态与共享底座。

界面上的阶段不是标签：每一步都对应 task_nodes 里的一行，带依赖、产出、
验收与重试次数。任一章失败只重跑该章，已通过的章节不再消耗 token。

与画布引擎（engine.py + workflow_runs）的分工：
- 任务页的文档生成走这里——逐节点发 SSE，前端时间线逐节点点亮；
- 画布自定义工作流走 engine.py——拓扑并行、approval 暂停、run 级额度。
两者共享 builtin.py 的六节点定义，但执行上下文不同（一个跟任务，一个跟 run）。
"""
from __future__ import annotations

import json
from typing import Awaitable, Callable

from ... import db, deps
from .. import llm, prompts, skills_loader

Emit = Callable[[str, dict], Awaitable[None]]

JSON_FIX_NOTE = (
    "下面这段不是合法 JSON。请把它重写成严格合法的 JSON："
    "只输出 JSON 本身，不要写解释、不要加围栏、不要写注释；"
    "字符串值里不要出现英文双引号，需要引用时用「」；不要在字符串里换行。\n\n"
)

MODE_NODES = {
    # chat = 普通对话：不走工作流，0 节点，任务页只显示对话流
    "chat": [],
    "research": ["intake", "source_digest", "outline", "draft", "qa"],
    "writing": ["intake", "source_digest", "outline", "draft", "render", "qa"],
    "slides": ["intake", "source_digest", "outline", "render", "qa"],
    "docs": ["intake", "source_digest", "outline", "draft", "render", "qa"],
}

NODE_LABELS = {
    "intake": "理解任务",
    "source_digest": "读材料",
    "outline": "搭结构",
    "draft": "写正文",
    "render": "生成文件",
    "qa": "校验交付",
}


def node_label(key: str) -> str:
    if key.startswith("draft.c"):
        return "写第 %s 节" % key.split(".c", 1)[1]
    return NODE_LABELS.get(key, key)


def ensure_nodes(task_id: str, mode: str) -> None:
    """按任务模式建固定节点集（幂等）。"""
    keys = MODE_NODES.get(mode, MODE_NODES["writing"])
    with db.tx() as conn:
        existing = {r["node_key"] for r in conn.execute("SELECT node_key FROM task_nodes WHERE task_id = ?", (task_id,))}
        seq = len(existing)
        for key in keys:
            if key in existing:
                continue
            node_deps = [] if key == "intake" else [keys[max(keys.index(key) - 1, 0)]] if key in keys else ["outline"]
            conn.execute(
                "INSERT OR IGNORE INTO task_nodes(task_id, node_key, seq, deps, status) VALUES(?,?,?,?,?)",
                (task_id, key, seq, json.dumps(node_deps), "todo"),
            )
            seq += 1


def ensure_workflow_nodes(task_id: str, meta: dict) -> None:
    """按流程 meta（技能快照或内置 PPT_WORKFLOW_META）建节点集（幂等）。"""
    with db.tx() as conn:
        existing = {r["node_key"] for r in conn.execute(
            "SELECT node_key FROM task_nodes WHERE task_id = ?", (task_id,))}
        for seq, node in enumerate(meta.get("nodes") or []):
            if node["key"] in existing:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO task_nodes(task_id, node_key, seq, deps, status) VALUES(?,?,?,?,?)",
                (task_id, node["key"], seq, json.dumps(node.get("deps") or []), "todo"),
            )


def reset_nodes(task_id: str, meta: dict) -> None:
    """重新生成前清空节点产出：状态回 todo，清产出与错误。"""
    for node in meta.get("nodes") or []:
        node_set(task_id, node["key"], "todo", output_ref=None, error=None, attempt=0)


def invalidate_downstream(task_id: str, key: str) -> None:
    """key 之后的节点整体作废：状态回 todo，清空产出与错误。"""
    anchor = node_get(task_id, key)
    if not anchor:
        return
    rows = db.rows(
        "SELECT node_key FROM task_nodes WHERE task_id = ? AND seq > ? ORDER BY seq",
        (task_id, anchor["seq"]),
    )
    for row in rows:
        node_set(task_id, row["node_key"], "todo", output_ref=None, error=None, attempt=0)


def node_get(task_id: str, key: str):
    return db.one("SELECT * FROM task_nodes WHERE task_id = ? AND node_key = ?", (task_id, key))


def node_set(task_id: str, key: str, status: str, **cols) -> None:
    allowed = {"input_ref", "output_ref", "acceptance", "version", "error", "attempt", "started_at", "ended_at"}
    fields = ["status = ?"]
    params: list = [status]
    for name, value in cols.items():
        if name in allowed:
            fields.append(f"{name} = ?")
            params.append(value)
    params.extend([task_id, key])
    db.execute(f"UPDATE task_nodes SET {', '.join(fields)} WHERE task_id = ? AND node_key = ?", params)


def nodes_list(task_id: str) -> list[dict]:
    rows = db.rows("SELECT * FROM task_nodes WHERE task_id = ? ORDER BY seq ASC", (task_id,))
    result = []
    for row in rows:
        result.append({
            "key": row["node_key"],
            "label": node_label(row["node_key"]),
            "status": row["status"],
            "attempt": row["attempt"],
            "deps": json.loads(row["deps"] or "[]"),
            "acceptance": json.loads(row["acceptance"]) if row["acceptance"] else None,
            "error": row["error"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
        })
    return result


def node_out(task_id: str, key: str) -> dict:
    row = node_get(task_id, key)
    if not row or not row["output_ref"]:
        return {}
    try:
        return json.loads(row["output_ref"])
    except json.JSONDecodeError:
        return {}


def save_node_out(task_id: str, key: str, payload: dict) -> None:
    node_set(task_id, key, "done", output_ref=json.dumps(payload, ensure_ascii=False), ended_at=db.now_iso())


async def emit_node(emit: Emit, task_id: str, key: str, status: str, **extra) -> None:
    payload = {"key": key, "label": node_label(key), "status": status}
    payload.update(extra)
    await emit("node", payload)


async def fail_node(emit: Emit, task_id: str, key: str, message: str) -> None:
    row = node_get(task_id, key)
    attempt = (row["attempt"] if row else 0) + 1
    node_set(task_id, key, "failed", error=message[:500], attempt=attempt, ended_at=db.now_iso())
    await emit_node(emit, task_id, key, "failed", error=message[:300])


def params_of(task) -> dict:
    try:
        return json.loads(task["params_json"] or "{}")
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


def record_usage(user_id: str, reply: dict) -> None:
    """每一次上游调用都要记账，否则配额形同虚设。"""
    usage = reply.get("usage") or {}
    deps.record_usage(
        user_id,
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        int(usage.get("cache_read_input_tokens") or 0),
    )


async def complete_json(system: str, prompt: str, user_id: str,
                        effort: str, max_tokens: int, model: str = "") -> dict:
    """拿结构 JSON。第一次解析失败时让模型把上一次的输出重写一遍，
    只重写不新增内容——比直接判任务失败划算得多。"""
    reply = await llm.complete(
        system, [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        effort=effort, max_tokens=max_tokens, model=model,
    )
    record_usage(user_id, reply)
    try:
        return llm.extract_json(reply["text"])
    except Exception:
        broken = (reply.get("text") or "")[:8000]
        fix = JSON_FIX_NOTE + broken
        reply2 = await llm.complete(
            system, [{"role": "user", "content": [{"type": "text", "text": fix}]}],
            effort=effort, max_tokens=max_tokens, model=model,
        )
        record_usage(user_id, reply2)
        return llm.extract_json(reply2["text"])


def system_for(user_id: str, mode: str, materials: str) -> str:
    skills = skills_loader.active_prompt_skills(user_id)
    return prompts.build_system(mode, skills, materials)


def register_deliverable(user_id: str, task_id: str | None, path, name: str, mime: str, report: dict) -> dict:
    """交付物落 files + 一条 system 消息（消息流里能看到下载入口，历史加载一致）。"""
    import hashlib

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    file_id = db.new_id("f_")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO files(id, user_id, task_id, kind, name, path, mime, size, sha256, meta_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (file_id, user_id, task_id, "deliverable", name, str(path), mime, path.stat().st_size,
             digest, json.dumps({"report": report}, ensure_ascii=False), db.now_iso()),
        )
        conn.execute(
            "INSERT INTO messages(task_id, role, content, status, created_at) VALUES(?,?,?,?,?)",
            (task_id, "system",
             json.dumps({"type": "deliverable", "file_id": file_id, "name": name,
                         "size": path.stat().st_size, "report": report}, ensure_ascii=False),
             "done", db.now_iso()),
        )
    return {"id": file_id, "name": name}


def touch_task(task_id: str, status: str) -> None:
    db.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (status, db.now_iso(), task_id))
