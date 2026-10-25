"""工作流执行引擎。

拓扑分层（Kahn）逐波推进，波内 `asyncio.gather` 并行。
每个节点独立事务——一个节点失败不影响同波其它节点。

额度语义：**run 开始 reserve 一次**，节点内的 LLM 只记账不扣次。
否则一个 6 节点的 PPT 工作流会把用户的每日额度吃掉 6 次。

审批节点：跑到 approval 就暂停，状态置 waiting_approval，
等 `POST /api/workflows/runs/{id}/resume` 批准后续跑。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ... import config, db
from .. import llm, prompts
from ..tools.executor import ToolError, execute_tool
from .gates import eval_gate
from .validate import topological_waves, validate_flow


@dataclass
class RunEvent:
    """引擎对外发的事件。router 把它转成 SSE。"""

    kind: str            # run | node | log | artifact | approval | done | error
    node_id: str = ""
    label: str = ""
    status: str = ""     # running | done | failed | waiting | skipped
    data: dict = field(default_factory=dict)


def graph_fingerprint(graph: dict) -> str:
    """整图指纹（移植 agent-flow executionFingerprint）。

    只哈希会影响**执行语义**的部分：节点 id/type/data 与边 id/source/target。
    画布拖拽产生的 position 不参与——那是布局不是逻辑，纳入它会让
    「只是挪了挪节点」也变成拒续跑。

    用途：审批挂起期间用户改了图，resume 会拿着旧输出配新逻辑跑，
    而界面显示「恢复成功」——结果错得无声无息。存 hash 只为比对。
    """
    nodes = []
    for n in (graph.get("nodes") or []):
        if not isinstance(n, dict) or not n.get("id"):
            continue
        nodes.append({
            "id": str(n.get("id")),
            "type": str(n.get("type") or ""),
            "data": n.get("data") or {},
        })
    edges = []
    for e in (graph.get("edges") or []):
        if not isinstance(e, dict):
            continue
        edges.append({
            "id": str(e.get("id") or ""),
            "source": str(e.get("source") or ""),
            "target": str(e.get("target") or ""),
        })
    nodes.sort(key=lambda x: x["id"])
    edges.sort(key=lambda x: (x["source"], x["target"], x["id"]))
    payload = json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class WorkflowError(Exception):
    pass


class ApprovalPause(Exception):
    """引擎在审批节点暂停。"""

    def __init__(self, node_id: str, label: str, reason: str):
        super().__init__(reason)
        self.node_id = node_id
        self.label = label
        self.reason = reason


def _node_map(graph: dict) -> dict[str, dict]:
    return {
        str(n.get("id")): n
        for n in (graph.get("nodes") or [])
        if isinstance(n, dict) and n.get("id")
    }


def _incoming(graph: dict) -> dict[str, list[str]]:
    incoming: dict[str, list[str]] = {nid: [] for nid in _node_map(graph)}
    for edge in (graph.get("edges") or []):
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if target in incoming:
            incoming[target].append(source)
    return incoming


def _render_template(template: str, variables: dict) -> str:
    """`{{var}}` 简单替换。未知变量原样保留（让模型自己看到占位符）。"""
    out = template or ""
    for key, value in (variables or {}).items():
        out = out.replace("{{" + str(key) + "}}", str(value if value is not None else ""))
    return out


async def _run_llm_node(node: dict, ctx: dict, run_id: str) -> dict:
    data = node.get("data") or {}
    prompt = _render_template(str(data.get("prompt") or ""), ctx["vars"])
    model = str(data.get("model") or "").strip() or None
    effort = str(data.get("effort") or "").strip() or None
    # max_tokens 来自画布节点数据，用户可以任意填写；不夹上限等于把单次成本交出去。
    # 上限统一取平台的聊天上限，下限 256 兜住异常小值。
    max_tokens = max(256, min(int(data.get("max_tokens") or 4000), config.MAX_TOKENS_CHAT))
    system = _render_template(str(data.get("system") or ""), ctx["vars"]) or prompts.SYSTEM_CORE
    try:
        reply = await llm.complete(
            system,
            [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            model=model, effort=effort, max_tokens=max_tokens,
        )
    except llm.UpstreamError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise WorkflowError(f"LLM 调用失败：{type(exc).__name__}") from exc
    text = reply.get("text") or ""
    # 节点内的 LLM 只记账，不扣次
    ctx["record_usage"](reply.get("usage") or {})
    return {"text": text, "model": reply.get("model") or model or ""}


class ToolApprovalRequired(Exception):
    """工具节点要求人工确认。

    不是失败——它和 approval 节点一样把整轮挂起，等 `resume` 带着批准回来重跑该节点。
    """

    def __init__(self, node_id: str, tool: str, reason: str = ""):
        super().__init__(reason or f"工具 {tool} 需要人工确认")
        self.node_id = node_id
        self.tool = tool
        self.reason = reason or f"工具 {tool} 需要人工确认"


async def _run_tool_node(node: dict, ctx: dict, run_id: str) -> dict:
    data = node.get("data") or {}
    node_id = str(node.get("id") or "")
    name = str(data.get("tool") or "").strip()
    tool_input = data.get("input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}
    tool_input = {
        k: _render_template(str(v), ctx["vars"]) if isinstance(v, str) else v
        for k, v in tool_input.items()
    }
    # approved 只可能来自两种场合：聊天回路里用户真点了批准，或 resume 明确带着批准回来。
    # 画布自动跑不许替用户点这个头（原先硬编码 True，等于绕过 doc_generate 的审批闸）。
    result = await execute_tool(
        name, tool_input,
        {"user_id": ctx["user_id"], "task_id": ctx.get("task_id"), "run_id": run_id},
        approved=bool(ctx.get("approved")),
    )
    if not result.ok:
        if result.needs_approval:
            raise ToolApprovalRequired(
                node_id, name, str((result.meta or {}).get("reason") or "")
            )
        raise ToolError(result.error or f"工具 {name} 执行失败")
    return {"output": result.output}


async def _run_rag_node(node: dict, ctx: dict, run_id: str) -> dict:
    from ..rag import search

    data = node.get("data") or {}
    query = _render_template(str(data.get("query") or data.get("prompt") or ""), ctx["vars"])
    top_k = int(data.get("top_k") or 5)
    collection_ids = ctx.get("rag_collections") or []
    hits = search(collection_ids, ctx["user_id"], query, top_k)
    return {"hits": hits, "text": "\n\n".join(h.get("body", "") for h in hits)}


async def _run_artifact_node(node: dict, ctx: dict, run_id: str) -> dict:
    from .. import artifacts

    data = node.get("data") or {}
    title = _render_template(str(data.get("title") or "工作流产物"), ctx["vars"])
    html = _render_template(str(data.get("html") or ""), ctx["vars"])
    if not html:
        raise WorkflowError("artifact 节点没有 html 内容")
    art_id = artifacts.create_artifact(
        ctx["user_id"], title, html, task_id=ctx.get("task_id"),
    )
    ctx["artifacts"].append(art_id)
    return {"artifact_id": art_id, "title": title}


def _gate_inputs(node: dict, ctx: dict, incoming: list[str]) -> list[bool]:
    """取上游节点的输出作为门输入。

    布尔判定：输出里的 "passed"/"ok"/true 字段为真，或输出文本以 true/yes/是 开头。
    """
    values: list[bool] = []
    for source in incoming:
        out = ctx["outputs"].get(source) or {}
        if isinstance(out, dict):
            if "passed" in out:
                values.append(bool(out["passed"]))
                continue
            if "ok" in out:
                values.append(bool(out["ok"]))
                continue
            if "result" in out and isinstance(out["result"], bool):
                values.append(bool(out["result"]))
                continue
        text = str(out.get("text") or out.get("output") or "").strip().lower()
        values.append(text in {"true", "yes", "是", "通过", "pass", "ok"})
    return values


async def _run_node(node: dict, ctx: dict, run_id: str) -> dict:
    node_type = str(node.get("type") or "")
    data = node.get("data") or {}
    label = str(data.get("label") or node.get("id") or "")
    incoming = ctx["incoming"].get(str(node.get("id")), [])

    if node_type == "start":
        return {"text": "", "started": True}

    if node_type == "llm":
        out = await _run_llm_node(node, ctx, run_id)
        ctx["vars"][str(node.get("id"))] = out.get("text", "")[:4000]
        ctx["vars"]["last"] = out.get("text", "")[:4000]
        return out

    if node_type == "tool":
        out = await _run_tool_node(node, ctx, run_id)
        ctx["vars"][str(node.get("id"))] = str(out.get("output"))[:4000]
        ctx["vars"]["last"] = str(out.get("output"))[:4000]
        return out

    if node_type == "rag":
        out = await _run_rag_node(node, ctx, run_id)
        ctx["vars"][str(node.get("id"))] = out.get("text", "")[:4000]
        ctx["vars"]["last"] = out.get("text", "")[:4000]
        return out

    if node_type == "artifact":
        out = await _run_artifact_node(node, ctx, run_id)
        ctx["vars"][str(node.get("id"))] = out.get("artifact_id", "")
        return out

    if node_type == "gate":
        op = str(data.get("op") or "AND").upper()
        inputs = _gate_inputs(node, ctx, incoming)
        try:
            result = eval_gate(op, inputs)
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
        return {"passed": result, "op": op, "inputs": inputs, "text": str(result)}

    if node_type == "approval":
        raise ApprovalPause(str(node.get("id")), label, str(data.get("reason") or "需要人工确认"))

    if node_type == "output":
        text = _render_template(str(data.get("text") or ""), ctx["vars"])
        if not text:
            text = ctx["vars"].get("last", "")
        return {"text": text[:8000]}

    raise WorkflowError(f"未知节点类型：{node_type}")


def _short_circuit(graph: dict, gate_result: bool, ctx: dict) -> None:
    """门为假时，跳过它的下游分支。

    只有门为假才跳——门为真时全部下游都跑（下游节点自己判断要不要用这个分支）。
    """
    return None


async def run_workflow(
    workflow_id: str,
    user_id: str,
    graph: dict,
    inputs: dict | None = None,
    *,
    task_id: str | None = None,
    run_id: str | None = None,
    start_node: str | None = None,
    resume_outputs: dict | None = None,
    skip_nodes: set[str] | None = None,
    approved: bool = False,
) -> AsyncIterator[RunEvent]:
    """跑一次工作流，边跑边发事件。

    这是个异步生成器：调用方 for-await 消费。

    `resume_outputs` + `skip_nodes` 供审批后续跑用：已跑完的节点带输出跳过，
    暂停的那个审批节点也跳过（它的输出合成 {"approved": True}）。

    `approved` 只对工具节点有效：resume 带着批准回来时才允许执行需要确认的工具。
    """
    errors = validate_flow(graph.get("nodes") or [], graph.get("edges") or [])
    if errors:
        detail = "；".join(f"{k}: {', '.join(v)}" for k, v in errors.items())
        yield RunEvent("error", error=detail)
        return

    run_id = run_id or db.new_id("wfr_")
    nodes = _node_map(graph)
    incoming = _incoming(graph)
    waves = topological_waves(graph.get("nodes") or [], graph.get("edges") or [])

    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0}

    def record_usage(u: dict) -> None:
        usage["input_tokens"] += int(u.get("input_tokens") or 0)
        usage["output_tokens"] += int(u.get("output_tokens") or 0)
        usage["cache_read"] += int(u.get("cache_read_input_tokens") or 0)

    ctx: dict[str, Any] = {
        "user_id": user_id,
        "task_id": task_id,
        "vars": dict(inputs or {}),
        "outputs": dict(resume_outputs or {}),
        "incoming": incoming,
        "artifacts": [],
        "record_usage": record_usage,
        "rag_collections": (inputs or {}).get("rag_collection_ids") or [],
        "approved": approved,
    }
    for key in ("rag_collection_ids",):
        ctx["vars"].pop(key, None)

    # 续跑：已跑完的节点整波跳过；被暂停的审批节点合成一个「已批准」输出，
    # 否则下游引用 {{confirm}} 的模板会拿到空值。
    skipped: set[str] = set(skip_nodes or set())
    for node_id in skipped:
        node = nodes.get(node_id) or {}
        if str(node.get("type")) == "approval":
            ctx["outputs"].setdefault(node_id, {"approved": True, "text": "已批准"})

    now = db.now_iso()
    fingerprint = graph_fingerprint(graph)
    has_hash = db.has_column("workflow_runs", "graph_hash")
    if not db.one("SELECT id FROM workflow_runs WHERE id = ?", (run_id,)):
        if has_hash:
            db.execute(
                "INSERT INTO workflow_runs(id, workflow_id, user_id, task_id, status, input_json, created_at, updated_at, graph_hash) "
                "VALUES(?,?,?,?,'running',?,?,?,?)",
                (run_id, workflow_id, user_id, task_id,
                 json.dumps(inputs or {}, ensure_ascii=False), now, now, fingerprint),
            )
        else:
            db.execute(
                "INSERT INTO workflow_runs(id, workflow_id, user_id, task_id, status, input_json, created_at, updated_at) "
                "VALUES(?,?,?,?,'running',?,?,?)",
                (run_id, workflow_id, user_id, task_id,
                 json.dumps(inputs or {}, ensure_ascii=False), now, now),
            )
    elif resume_outputs is not None and has_hash:
        # 续跑的第二道闸：图在暂停期间被改过 → 旧输出配新逻辑，拒跑。
        # router 的 resume 入口已经拦过一道，这里兜住其他调用方。
        row = db.one("SELECT graph_hash FROM workflow_runs WHERE id = ?", (run_id,))
        stored = (row["graph_hash"] if row else "") or ""
        if stored and stored != fingerprint:
            yield RunEvent("error", status="stale", data={
                "message": "流程已在暂停期间被修改，请重新运行",
                "run_id": run_id,
            })
            return

    yield RunEvent("run", status="running", data={"run_id": run_id, "workflow_id": workflow_id})

    final_output: dict = {}
    failed = False

    for wave_index, wave in enumerate(waves):
        # wave 里是节点 id，先换成节点本体；整波被跳过则不进
        wave_nodes = [nodes[n] for n in wave if n in nodes and n not in skipped]
        if not wave_nodes:
            continue

        # 审批节点不能并行跑（它会中断整轮），先单独处理
        approval_nodes = [n for n in wave_nodes if str(n.get("type")) == "approval"]
        if approval_nodes:
            node = approval_nodes[0]
            node_id = str(node.get("id"))
            label = str((node.get("data") or {}).get("label") or node_id)
            reason = str((node.get("data") or {}).get("reason") or "需要人工确认")
            db.execute(
                "UPDATE workflow_runs SET status = 'waiting_approval', paused_node = ?, "
                "updated_at = ? WHERE id = ?",
                (node_id, db.now_iso(), run_id),
            )
            yield RunEvent("approval", node_id=node_id, label=label, status="waiting",
                           data={"reason": reason, "run_id": run_id})
            return

        results = await asyncio.gather(
            *(_safe_node(node, ctx, run_id) for node in wave_nodes),
            return_exceptions=True,
        )

        # 工具节点要求人工确认：和审批节点一样挂起整轮，等 resume 带批准回来
        pending = next(
            (o for o in results if isinstance(o, ToolApprovalRequired)), None
        )
        if pending is not None:
            node = nodes.get(pending.node_id) or {}
            label = str((node.get("data") or {}).get("label") or pending.node_id)
            db.execute(
                "UPDATE workflow_runs SET status = 'waiting_approval', paused_node = ?, "
                "updated_at = ? WHERE id = ?",
                (pending.node_id, db.now_iso(), run_id),
            )
            yield RunEvent("approval", node_id=pending.node_id, label=label, status="waiting",
                           data={"reason": pending.reason, "run_id": run_id,
                                 "tool": pending.tool})
            return

        for node, outcome in zip(wave_nodes, results):
            node_id = str(node.get("id"))
            label = str((node.get("data") or {}).get("label") or node_id)
            if isinstance(outcome, BaseException):
                failed = True
                message = str(outcome)
                db.execute(
                    "UPDATE workflow_run_nodes SET status = 'failed', error = ?, ended_at = ? "
                    "WHERE run_id = ? AND node_id = ?",
                    (message, db.now_iso(), run_id, node_id),
                )
                yield RunEvent("node", node_id=node_id, label=label, status="failed",
                               data={"error": message})
                continue
            ctx["outputs"][node_id] = outcome
            yield RunEvent("node", node_id=node_id, label=label, status="done",
                           data={"output": _summarize(outcome)})
            if str(node.get("type")) == "output":
                final_output = outcome

    status = "failed" if failed else "done"
    db.execute(
        "UPDATE workflow_runs SET status = ?, output_json = ?, updated_at = ? WHERE id = ?",
        (status, json.dumps(final_output, ensure_ascii=False)[:100000], db.now_iso(), run_id),
    )
    yield RunEvent("done", status=status, data={
        "run_id": run_id,
        "output": final_output,
        "artifacts": ctx["artifacts"],
        "usage": usage,
    })


def _summarize(output: dict) -> Any:
    """节点输出摘要：只给前端看，不塞整个大对象。"""
    if not isinstance(output, dict):
        return str(output)[:500]
    if "text" in output:
        return str(output["text"])[:500]
    if "output" in output:
        return str(output["output"])[:500]
    return {k: v for k, v in list(output.items())[:6]}


async def _safe_node(node: dict, ctx: dict, run_id: str) -> dict:
    node_id = str(node.get("id"))
    label = str((node.get("data") or {}).get("label") or node_id)
    db.execute(
        "INSERT INTO workflow_run_nodes(run_id, node_id, node_type, label, status, started_at) "
        "VALUES(?,?,?,?, 'running', ?)",
        (run_id, node_id, str(node.get("type") or ""), label, db.now_iso()),
    )
    try:
        result = await _run_node(node, ctx, run_id)
    except Exception:
        db.execute(
            "UPDATE workflow_run_nodes SET status = 'failed', ended_at = ? WHERE run_id = ? AND node_id = ? "
            "AND status = 'running'",
            (db.now_iso(), run_id, node_id),
        )
        raise
    db.execute(
        "UPDATE workflow_run_nodes SET status = 'done', output_json = ?, ended_at = ? "
        "WHERE run_id = ? AND node_id = ? AND status = 'running'",
        (json.dumps(result, ensure_ascii=False, default=str)[:200000],
         db.now_iso(), run_id, node_id),
    )
    return result
