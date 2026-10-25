"""任务 CRUD。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import config, db, deps
from ..services import models, workflow

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

MODES = {"chat", "research", "writing", "slides", "docs"}


class TaskIn(BaseModel):
    mode: str = "writing"
    goal: str = Field(min_length=2, max_length=4000)
    title: str | None = None
    params: dict | None = None


class TaskPatch(BaseModel):
    title: str | None = None
    goal: str | None = None
    params: dict | None = None


def _own_task(request: Request, task_id: str):
    user = deps.current_user(request)
    row = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["user_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权访问这个任务")
    return user, row


def _json_list(raw) -> list:
    """把消息里的 JSON 文本列安全解析成 list（脏数据不该让整个详情页 500）。"""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json_obj(raw) -> dict:
    """把 JSON 文本列安全解析成 dict（脏数据不该让整个详情页 500）。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _summary(row) -> dict:
    counts = db.one(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed FROM task_nodes WHERE task_id = ?",
        (row["id"],),
    )
    # params 必须回传：前端刷新页面后靠它还原模型/思考档位/计划模式，
    # 缺了这个字段，用户切过的模型在刷新后就没了。
    try:
        params = json.loads(row["params_json"] or "{}")
    except (json.JSONDecodeError, TypeError, KeyError, IndexError):
        params = {}
    return {
        "id": row["id"],
        "mode": row["mode"],
        "title": row["title"],
        "goal": row["goal"],
        "status": row["status"],
        "stage": row["stage"],
        "params": params if isinstance(params, dict) else {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "nodes_total": counts["total"] or 0,
        "nodes_done": counts["done"] or 0,
        "nodes_failed": counts["failed"] or 0,
    }


@router.post("")
def create_task(payload: TaskIn, request: Request):
    user = deps.current_user(request)
    mode = payload.mode if payload.mode in MODES else "writing"
    goal = payload.goal.strip()
    title = (payload.title or goal.split("\n")[0])[:24].strip() or "未命名任务"
    task_id = db.new_id("t_")
    # 新任务继承访客的默认模型，写进 params_json 当行为快照：
    # 之后改「默认模型」不会追溯改动已建任务，符合"参数快照"语义。
    default_model = user.get("pref_model")
    if default_model not in models.BY_ID:
        default_model = models.default_model_id()
    seed = {"model": default_model}
    params = dict(payload.params or {})
    # skill 快照（{id,name,version}）只能由服务端在生成启动时按 published_version 写入，
    # 建任务时客户端只允许传 skill_id 绑定关系
    params.pop("skill", None)
    # V3.2：prompt 技能绑定白名单。客户端只能传 id 列表，服务端逐个校验后落库；
    # 注入在 chat 时实时取（停用即不注入），这里只存绑定关系不做正文快照。
    raw_prompt_ids = params.pop("prompt_skill_ids", None)
    if raw_prompt_ids is not None:
        if not isinstance(raw_prompt_ids, list) or len(raw_prompt_ids) > 5:
            raise HTTPException(status_code=400, detail="prompt_skill_ids 必须是列表且最多绑定 5 个技能")
        cleaned = []
        for sid in raw_prompt_ids:
            if not isinstance(sid, str) or not sid.strip():
                continue
            srow = db.one("SELECT kind, enabled FROM skills WHERE id = ?", (sid,))
            if not srow or srow["kind"] != "prompt" or not srow["enabled"]:
                raise HTTPException(status_code=400, detail=f"技能不可绑定：{sid}")
            if sid not in cleaned:
                cleaned.append(sid)
        params["prompt_skill_ids"] = cleaned
    seed.update(params)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO tasks(id, user_id, mode, title, goal, status, stage, params_json, created_at, updated_at) "
            "VALUES(?,?,?,?,?,'active','intake',?,?,?)",
            (task_id, user["id"], mode, title, goal,
             json.dumps(seed, ensure_ascii=False), db.now_iso(), db.now_iso()),
        )
    if mode == "slides":
        # PPT 走六节点工作流：节点集按技能快照（或内置流程）建，不插旧 intake/outline 链
        row = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        meta, _snap = workflow.load_flow(row)
        workflow.ensure_workflow_nodes(task_id, meta)
    else:
        workflow.ensure_nodes(task_id, mode)
    return {"ok": True, "task": _summary(db.one("SELECT * FROM tasks WHERE id = ?", (task_id,)))}


@router.get("")
def list_tasks(request: Request, limit: int = 30, offset: int = 0):
    user = deps.current_user(request)
    rows = db.rows(
        "SELECT * FROM tasks WHERE user_id = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        (user["id"], min(limit, 100), offset),
    )
    return {"tasks": [_summary(r) for r in rows]}


@router.get("/{task_id}")
def get_task(task_id: str, request: Request):
    user, row = _own_task(request, task_id)
    files = db.rows(
        "SELECT id, kind, name, mime, size, meta_json, created_at FROM files "
        "WHERE task_id = ? ORDER BY created_at ASC", (task_id,),
    )
    messages = db.rows(
        "SELECT id, role, content, thinking, tokens_in, tokens_out, cache_read, stop_reason, status, "
        "plan_flag, skills, created_at"
        + (", web_flag, web_sources" if db.has_column("messages", "web_flag") else "")
        # P1-4/P1-5：只加字段不改字段——老库里这三列缺失时对应项给默认值，
        # 前端切换器/停止卡拿不到坐标就退化成「不显示」。
        + (", stopped, parent_id, branch_index" if db.has_column("messages", "stopped") else "")
        + " FROM messages WHERE task_id = ? ORDER BY id ASC LIMIT 200", (task_id,),
    )
    # waiting_approval 的大纲要能刷新页面后还在：从 content 节点产出里重建只读视图
    approval = None
    if row["status"] == "waiting_approval":
        content_row = db.one(
            "SELECT output_ref FROM task_nodes WHERE task_id = ? AND node_key = 'content'", (task_id,)
        )
        if content_row and content_row["output_ref"]:
            try:
                structure = json.loads(content_row["output_ref"])
                approval = {
                    "title": structure.get("title", ""),
                    "subtitle": structure.get("subtitle", ""),
                    "slides": [
                        {"index": i, "title": s.get("title", ""), "layout": s.get("layout") or "bullets"}
                        for i, s in enumerate(structure.get("slides") or [], 1)
                        if isinstance(s, dict)
                    ],
                }
            except json.JSONDecodeError:
                approval = None
    return {
        "task": _summary(row),
        "nodes": workflow.nodes_list(task_id),
        # 待批准的工具（刷新页面后前端还能提示用户去批）
        # _json_obj 对 NULL 返回 {}，而前端把 {} 当 truthy 会渲染出空白审批卡 —— 空就给 None
        "tool_pending": (_json_obj(row["tool_pending"]) or None) if "tool_pending" in row.keys() else None,
        "files": [
            {
                "id": f["id"], "kind": f["kind"], "name": f["name"], "mime": f["mime"],
                "size": f["size"], "meta": json.loads(f["meta_json"] or "{}"), "created_at": f["created_at"],
            }
            for f in files
        ],
        "messages": [
            {
                "id": m["id"], "role": m["role"], "content": m["content"], "thinking": m["thinking"],
                "tokens_in": m["tokens_in"], "tokens_out": m["tokens_out"], "cache_read": m["cache_read"],
                "stop_reason": m["stop_reason"], "status": m["status"], "created_at": m["created_at"],
                "plan_flag": bool(m["plan_flag"]), "skills": m["skills"] or "",
                "web_flag": bool(m["web_flag"]) if "web_flag" in m.keys() else False,
                "web_sources": _json_list(m["web_sources"]) if "web_sources" in m.keys() else [],
                "stopped": bool(m["stopped"]) if "stopped" in m.keys() else False,
                "parent_id": m["parent_id"] if "parent_id" in m.keys() else None,
                "branch_index": m["branch_index"] if "branch_index" in m.keys() else None,
            }
            for m in messages
        ],
        "params": json.loads(row["params_json"] or "{}"),
        "approval": approval,
    }


@router.patch("/{task_id}")
def patch_task(task_id: str, payload: TaskPatch, request: Request):
    user, row = _own_task(request, task_id)
    fields, params = [], []
    if payload.title is not None:
        fields.append("title = ?")
        params.append(payload.title.strip()[:40] or row["title"])
    if payload.goal is not None:
        fields.append("goal = ?")
        params.append(payload.goal.strip()[:4000])
    if payload.params is not None:
        # 必须是 merge 而不是整块替换：前端切换模型时只发 {model, effort}，
        # 整块替换会把 word_count / format / template_id 冲掉，
        # 后续生成的字数校验会读到 0 被静默跳过。
        try:
            current = json.loads(row["params_json"] or "{}")
        except json.JSONDecodeError:
            current = {}
        merged = {**current, **payload.params}
        fields.append("params_json = ?")
        params.append(json.dumps(merged, ensure_ascii=False))
    if not fields:
        return {"ok": True}
    fields.append("updated_at = ?")
    params.append(db.now_iso())
    params.append(task_id)
    db.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", params)
    return {"ok": True}


@router.delete("/{task_id}")
def delete_task(task_id: str, request: Request):
    user, row = _own_task(request, task_id)
    files = db.rows("SELECT path FROM files WHERE task_id = ?", (task_id,))
    with db.tx() as conn:
        conn.execute("DELETE FROM messages WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_nodes WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM files WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    for item in files:
        _safe_unlink(Path(item["path"]))
    return {"ok": True}


def _safe_unlink(path: Path) -> None:
    try:
        resolved = path.resolve()
        if config.DATA_DIR in resolved.parents and resolved.is_file():
            resolved.unlink()
    except Exception:
        pass
