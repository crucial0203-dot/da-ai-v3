"""V4.0 N9 工作流路由：定义、校验、运行、恢复。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import db, deps
from ..services.workflow import builtin, engine, validate

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


class GraphIn(BaseModel):
    name: str = Field(default="未命名工作流", max_length=80)
    description: str = Field(default="", max_length=300)
    graph: dict = Field(default_factory=dict)


class RunIn(BaseModel):
    inputs: dict = Field(default_factory=dict)
    task_id: str | None = None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("")
def list_workflows(request: Request):
    user = deps.current_user(request)
    rows = db.rows(
        "SELECT id, name, description, builtin, published_version, created_at, updated_at "
        "FROM workflows WHERE user_id = ? OR user_id = 'system' ORDER BY builtin DESC, updated_at DESC",
        (user["id"],),
    )
    out = []
    for row in rows:
        version = db.one(
            "SELECT graph_json FROM workflow_versions WHERE workflow_id = ? "
            "ORDER BY id DESC LIMIT 1", (row["id"],)
        )
        out.append({
            "id": row["id"], "name": row["name"], "description": row["description"],
            "builtin": bool(row["builtin"]), "published_version": row["published_version"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "graph": json.loads(version["graph_json"]) if version else {"nodes": [], "edges": []},
        })
    return {"workflows": out, "builtin": builtin.builtin_workflows()}


@router.post("/validate")
def validate_graph(payload: GraphIn, request: Request):
    """只校验不保存。前端在保存前先跑这个，错误直接高亮到节点。"""
    deps.current_user(request)
    errors = validate.validate_flow(
        payload.graph.get("nodes") or [], payload.graph.get("edges") or []
    )
    return {"ok": not errors, "errors": errors}


@router.post("")
def create_workflow(payload: GraphIn, request: Request):
    user = deps.current_user(request)
    errors = validate.validate_flow(
        payload.graph.get("nodes") or [], payload.graph.get("edges") or []
    )
    if errors:
        detail = "；".join(f"{k}: {', '.join(v)}" for k, v in errors.items())
        raise HTTPException(status_code=400, detail=f"工作流校验未通过：{detail}")
    wf_id = db.new_id("wf_")
    now = db.now_iso()
    db.execute(
        "INSERT INTO workflows(id, user_id, name, description, builtin, created_at, updated_at) "
        "VALUES(?,?,?,?,0,?,?)",
        (wf_id, user["id"], payload.name.strip() or "未命名工作流",
         payload.description.strip(), now, now),
    )
    db.execute(
        "INSERT INTO workflow_versions(workflow_id, version, graph_json, status, created_at) "
        "VALUES(?, '1.0', ?, 'draft', ?)",
        (wf_id, json.dumps(payload.graph, ensure_ascii=False), now),
    )
    return {"ok": True, "id": wf_id}


def _guard_workflow(request: Request, workflow_id: str) -> dict | None:
    """统一的权责门：属主、内置工作流可访问；其余一律 403。

    原先 run 分支漏了这道校验（GET/PUT/DELETE 都有），等于任何人拿到 id
    就能执行别人的私有工作流并把节点产出读回去。这里一次收口，四个分支共用。

    返回 row；内置工作流尚未落库时返回 None（由调用方用内置定义兜底）。
    """
    user = deps.current_user(request)
    row = db.one("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
    if row is not None:
        if row["user_id"] in (user["id"], "system") or row["builtin"] == 1:
            return row
        raise HTTPException(status_code=403, detail="无权访问这个工作流")
    # 表里没有：内置定义里有的放行（尚未落库），其余才是真的不存在
    if any(spec["id"] == workflow_id for spec in builtin.builtin_workflows()):
        return None
    raise HTTPException(status_code=404, detail="工作流不存在")


@router.put("/{workflow_id}")
def save_workflow(workflow_id: str, payload: GraphIn, request: Request):
    """保存新版本。服务端强校验，不通过就 400。"""
    user = deps.current_user(request)
    row = _guard_workflow(request, workflow_id)
    if row["user_id"] != user["id"] and row["builtin"] == 1:
        raise HTTPException(status_code=403, detail="内置工作流不可修改")
    if row["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="无权修改这个工作流")
    errors = validate.validate_flow(
        payload.graph.get("nodes") or [], payload.graph.get("edges") or []
    )
    if errors:
        detail = "；".join(f"{k}: {', '.join(v)}" for k, v in errors.items())
        raise HTTPException(status_code=400, detail=f"工作流校验未通过：{detail}")
    last = db.one(
        "SELECT MAX(id) AS i FROM workflow_versions WHERE workflow_id = ?", (workflow_id,)
    )
    version_num = 1
    if last and last["i"]:
        prev = db.one("SELECT version FROM workflow_versions WHERE id = ?", (last["i"],))
        if prev:
            try:
                version_num = int(str(prev["version"]).split(".")[0]) + 1
            except (ValueError, TypeError):
                version_num = 2
    now = db.now_iso()
    db.execute(
        "INSERT INTO workflow_versions(workflow_id, version, graph_json, status, created_at) "
        "VALUES(?,?,?, 'draft', ?)",
        (workflow_id, f"{version_num}.0",
         json.dumps(payload.graph, ensure_ascii=False), now),
    )
    db.execute(
        "UPDATE workflows SET name = ?, description = ?, updated_at = ? WHERE id = ?",
        (payload.name.strip() or row["name"], payload.description.strip(), now, workflow_id),
    )
    return {"ok": True, "version": f"{version_num}.0"}


@router.delete("/{workflow_id}")
def delete_workflow(workflow_id: str, request: Request):
    user = deps.current_user(request)
    row = _guard_workflow(request, workflow_id)
    if row["builtin"] == 1:
        raise HTTPException(status_code=400, detail="内置工作流不能删除")
    if row["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="无权删除这个工作流")
    with db.tx() as conn:
        conn.execute("DELETE FROM workflow_versions WHERE workflow_id = ?", (workflow_id,))
        conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
    return {"ok": True}


@router.post("/{workflow_id}/run")
async def run_workflow(workflow_id: str, payload: RunIn, request: Request):
    """跑工作流，SSE 流式返回节点事件。"""
    user = deps.current_user(request)
    row = _guard_workflow(request, workflow_id)
    graph: dict | None = None
    if row:
        version = db.one(
            "SELECT graph_json FROM workflow_versions WHERE workflow_id = ? "
            "ORDER BY id DESC LIMIT 1", (workflow_id,)
        )
        if version:
            graph = json.loads(version["graph_json"])
    if graph is None:
        # 内置工作流还没落库时，从内置定义取
        for spec in builtin.builtin_workflows():
            if spec["id"] == workflow_id:
                graph = spec["graph"]
                break
    if graph is None:
        raise HTTPException(status_code=404, detail="工作流不存在或没有可用版本")

    # 画布跑图也是一次 LLM 调用：限流 + 预扣缺一不可（原先两者都漏了，
    # 等于开了一条绕开配额与频率限制的通道）
    deps.limit_chat(user["id"])
    call_id, reason = deps.reserve_call(user["id"], "workflow")
    if not call_id:
        detail = {
            "running": "上一个请求还没结束，请稍候再试",
            "quota": "今天的额度用完了",
        }.get(reason, "暂时无法执行")
        raise HTTPException(status_code=429, detail=detail)

    async def stream():
        ok = False
        try:
            yield _sse("meta", {"workflow_id": workflow_id})
            async for event in engine.run_workflow(
                workflow_id, user["id"], graph, payload.inputs, task_id=payload.task_id
            ):
                yield _sse(event.kind, {
                    "node_id": event.node_id, "label": event.label,
                    "status": event.status, **event.data,
                })
            ok = True
        except Exception as exc:  # noqa: BLE001
            yield _sse("error", {"message": f"工作流执行失败：{type(exc).__name__}: {exc}"})
        finally:
            # 预扣必须落地：留着 reserved 会让该用户 600s 内所有请求被拒，且额度被扣。
            # settle_call 对未知/已结算的 request_id 天然幂等。
            try:
                deps.settle_call(call_id, ok)
            except Exception:  # noqa: BLE001
                pass

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request):
    user = deps.current_user(request)
    row = db.one("SELECT * FROM workflow_runs WHERE id = ?", (run_id,))
    if not row:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    if row["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="无权查看这个运行记录")
    nodes = db.rows(
        "SELECT node_id, node_type, label, status, wave, error, started_at, ended_at, output_json "
        "FROM workflow_run_nodes WHERE run_id = ? ORDER BY id",
        (run_id,),
    )
    return {
        "run": {
            "id": row["id"], "workflow_id": row["workflow_id"], "status": row["status"],
            "input": json.loads(row["input_json"] or "{}"),
            "output": json.loads(row["output_json"] or "{}"),
            "error": row["error"], "created_at": row["created_at"],
        },
        "nodes": [dict(n) for n in nodes],
    }


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: str, payload: RunIn, request: Request):
    """审批通过后续跑。"""
    user = deps.current_user(request)
    row = db.one("SELECT * FROM workflow_runs WHERE id = ?", (run_id,))
    if not row:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    if row["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="无权操作这个运行记录")
    version = db.one(
        "SELECT graph_json FROM workflow_versions WHERE workflow_id = ? "
        "ORDER BY id DESC LIMIT 1", (row["workflow_id"],)
    )
    if not version:
        raise HTTPException(status_code=404, detail="工作流定义已不存在")
    graph = json.loads(version["graph_json"])
    # S1-4：防陈旧提交。暂停期间保存过新版本的话，这里取到的是新图，
    # 而 outputs 是旧图跑出来的——直接续跑会把两者拼成一个错误结果，
    # 界面还显示「恢复成功」。指纹不一致就 409，让用户重新运行。
    stored = (row["graph_hash"] if "graph_hash" in row.keys() else "") or ""
    if stored and stored != engine.graph_fingerprint(graph):
        raise HTTPException(
            status_code=409,
            detail="流程已在暂停期间被修改，请重新运行",
        )
    outputs = {
        n["node_id"]: json.loads(n["output_json"] or "{}")
        for n in db.rows(
            "SELECT node_id, output_json FROM workflow_run_nodes WHERE run_id = ? AND status = 'done'",
            (run_id,),
        )
    }
    skip = set(outputs.keys())
    paused = (row["paused_node"] if "paused_node" in row.keys() else "") or ""
    # 跳过已跑完的节点 + 之前暂停的那个审批节点（否则会在原地再次暂停）。
    # 工具节点例外：它被暂停时还没执行，resume 要带批准把它重跑一遍。
    paused_node: dict | None = None
    if paused:
        paused_node = next(
            (n for n in (graph.get("nodes") or []) if str(n.get("id")) == paused), None
        )
    paused_is_tool = str((paused_node or {}).get("type") or "") == "tool"
    if paused and not paused_is_tool:
        skip.add(paused)
    db.execute(
        "UPDATE workflow_runs SET status = 'running', paused_node = '', updated_at = ? WHERE id = ?",
        (db.now_iso(), run_id),
    )

    # 续跑同样是一次调用通道：限流与预扣不能漏（否则可以反复 resume 白嫖）
    deps.limit_chat(user["id"])
    call_id, reason = deps.reserve_call(user["id"], "workflow")
    if not call_id:
        detail = {
            "running": "上一个请求还没结束，请稍候再试",
            "quota": "今天的额度用完了",
        }.get(reason, "暂时无法执行")
        raise HTTPException(status_code=429, detail=detail)

    async def stream():
        ok = False
        try:
            async for event in engine.run_workflow(
                row["workflow_id"], user["id"], graph,
                json.loads(row["input_json"] or "{}"),
                task_id=row["task_id"], run_id=run_id, resume_outputs=outputs,
                skip_nodes=skip, approved=paused_is_tool,
            ):
                yield _sse(event.kind, {
                    "node_id": event.node_id, "label": event.label,
                    "status": event.status, **event.data,
                })
            ok = True
        except Exception as exc:  # noqa: BLE001
            yield _sse("error", {"message": f"工作流恢复失败：{type(exc).__name__}: {exc}"})
        finally:
            try:
                deps.settle_call(call_id, ok)
            except Exception:  # noqa: BLE001
                pass

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/gate-table")
def gate_table(request: Request):
    deps.current_user(request)
    from ..services.workflow.gates import gate_truth_table

    return {"gates": gate_truth_table()}
