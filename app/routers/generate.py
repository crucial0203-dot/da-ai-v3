"""文档生成与节点重试，全部走 SSE，节点状态实时推到前端。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import config, db, deps
from ..services import capabilities, docx_builder, workflow
from .chat import sse

router = APIRouter(prefix="/api/tasks", tags=["generate"])


def _require_capabilities(task_mode: str, payload: "GenerateIn") -> None:
    """能力门槛必须在预扣额度之前判：不支持就 501，不能先扣了额度再报错。"""
    if task_mode == "slides":
        capabilities.require("pptx.fill_template" if payload.template_id else "pptx.create")
    else:
        capabilities.require("docx.create")
    if payload.format_sample_id:
        capabilities.require("docx.read_format_sample")


class GenerateIn(BaseModel):
    doc_type: str | None = None
    goal: str | None = None
    word_count: int | None = None
    page_count: int | None = None
    effort: str | None = None
    model: str | None = None
    format: dict | None = None
    format_sample_id: str | None = None
    template_id: str | None = None


def _own(request: Request, task_id: str):
    user = deps.current_user(request)
    task = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="无权访问这个任务")
    return user, task


def _sse_stream(factory: Callable[[Callable], Awaitable[dict]]) -> AsyncIterator[str]:
    async def generator() -> AsyncIterator[str]:
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(event: str, data: dict) -> None:
            await queue.put((event, data))

        async def runner() -> None:
            try:
                result = await factory(emit)
                await queue.put(("result", result))
            except Exception as exc:  # noqa: BLE001
                await queue.put(("error", {"message": f"{type(exc).__name__}: {exc}"}))
            finally:
                await queue.put(("end", None))

        handle = asyncio.create_task(runner())
        try:
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=8.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event == "end":
                    break
                yield sse(event, data)
        except (asyncio.CancelledError, GeneratorExit):
            if not handle.done():
                handle.cancel()
            raise
        finally:
            if not handle.done():
                handle.cancel()

    return generator()


def _headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }


def _resolve_workflow_skill(skill_id: str | None) -> dict | None:
    """按服务端数据解析技能快照：只认已发布且启用的 workflow 技能。"""
    if not skill_id:
        return None
    row = db.one(
        "SELECT id, name, published_version FROM skills "
        "WHERE id = ? AND kind = 'workflow' AND enabled = 1 AND published_version IS NOT NULL",
        (skill_id,),
    )
    if not row:
        return None
    return {"id": row["id"], "name": row["name"], "version": row["published_version"]}


@router.post("/{task_id}/generate")
async def generate(task_id: str, payload: GenerateIn, request: Request):
    user, task = _own(request, task_id)
    _require_capabilities(task["mode"], payload)
    deps.limit_chat(user["id"])
    if not config.MINIMAX_API_KEY:
        raise HTTPException(status_code=503, detail="服务端还没有配置模型 Key")

    stored = json.loads(task["params_json"] or "{}")
    # 版本快照只能由服务端按 published_version 解析，访客传什么 skill 键都不算数。
    # 预扣额度之前判：绑定的技能下线/未发布就 400，不能先扣了次数再报错。
    skill_id = stored.get("skill_id")
    skill_snapshot: dict | None = None
    if skill_id is not None:
        skill_snapshot = _resolve_workflow_skill(str(skill_id))
        if skill_snapshot is None:
            raise HTTPException(status_code=400, detail="任务绑定的技能不可用或已下线，请解除绑定后重试")

    call_id, reason = deps.reserve_call(user["id"], "generate")
    if not call_id:
        if reason == "running":
            raise HTTPException(status_code=409, detail="上一个生成还在跑，等它结束再发。")
        raise HTTPException(
            status_code=429,
            detail={
                "message": f"你今天 {deps.quota_for(user)} 次的额度用完了，明天自动恢复。",
                "code": "quota_exhausted",
                "quota": deps.quota_for(user),
                "used": deps.used_calls_today(user["id"]),
            },
        )

    data = payload.model_dump(exclude_none=True)
    # 参数要落盘：单章重跑与「只重排版」都从这里取目标字数/格式，
    # 不写回去的话那两处会读到空参数，字数校验被静默跳过
    stored.update({key: value for key, value in data.items() if key != "goal"})
    stored.pop("skill", None)
    if skill_snapshot:
        stored["skill"] = skill_snapshot
    db.execute(
        "UPDATE tasks SET status = 'running', params_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(stored, ensure_ascii=False), db.now_iso(), task_id),
    )

    async def factory(emit):
        try:
            # V3.1 路由：pptx 一律走六节点工作流（渲染前等确认）；docx 走原管线
            doc_type = data.get("doc_type") or ("pptx" if task["mode"] == "slides" else "docx")
            if doc_type == "pptx":
                result = await workflow.generate_ppt(user, task, data, emit)
            else:
                result = await workflow.generate_task(user, task, data, emit)
        except BaseException:
            # 含 CancelledError：客户端中途挂断也要把预扣退掉，否则额度会挂 10 分钟
            deps.settle_call(call_id, False)
            raise
        # token 由执行链逐章记账，这里只结算「算一次额度」
        deps.settle_call(call_id, bool(result.get("ok")))
        return result

    return StreamingResponse(_sse_stream(factory), media_type="text/event-stream", headers=_headers())


@router.post("/{task_id}/nodes/{node_key}/retry")
async def retry_node(task_id: str, node_key: str, request: Request):
    user, task = _own(request, task_id)
    deps.limit_chat(user["id"])

    call_id, reason = deps.reserve_call(user["id"], "retry")
    if not call_id:
        if reason == "running":
            raise HTTPException(status_code=409, detail="上一个生成还在跑，等它结束再发。")
        raise HTTPException(
            status_code=429,
            detail={
                "message": f"你今天 {deps.quota_for(user)} 次的额度用完了，明天自动恢复。",
                "code": "quota_exhausted",
                "quota": deps.quota_for(user),
                "used": deps.used_calls_today(user["id"]),
            },
        )

    async def factory(emit):
        try:
            result = await workflow.rerun_node(user, task, node_key, emit)
        except BaseException:
            deps.settle_call(call_id, False)
            raise
        deps.settle_call(call_id, bool(result.get("ok")))
        if result.get("ok"):
            rebuilt = workflow.assemble_from_nodes(task_id)
            if rebuilt:
                params = json.loads(task["params_json"] or "{}")
                fmt = docx_builder.merge_format(params.get("format") or {})
                out_path = config.FILES_DIR / user["id"] / f"{db.new_id('dl_')}.docx"
                docx_builder.build_docx(rebuilt, out_path, fmt)
                report = docx_builder.verify_docx(out_path, fmt, params.get("word_count") or 0,
                                                  len(rebuilt["sections"]))
                file_row = workflow.register_deliverable(
                    user["id"], task_id, out_path, rebuilt["title"] + ".docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document", report,
                )
                await emit("render", {"file_id": file_row["id"], "report": report})
                result["file_id"] = file_row["id"]
                result["report"] = report
        return result

    return StreamingResponse(_sse_stream(factory), media_type="text/event-stream", headers=_headers())


@router.post("/{task_id}/render")
async def render_only(task_id: str, payload: GenerateIn, request: Request):
    """不重新调用模型，只按现有章节重新排版出文件。"""
    user, task = _own(request, task_id)
    capabilities.require("docx.create")
    if payload.format_sample_id:
        capabilities.require("docx.read_format_sample")
    rebuilt = workflow.assemble_from_nodes(task_id)
    if not rebuilt:
        raise HTTPException(status_code=400, detail="还没有完整的章节内容，请先生成")

    def build() -> dict:
        fmt = docx_builder.merge_format(payload.format or {})
        if payload.format_sample_id:
            sample = db.one("SELECT * FROM files WHERE id = ? AND user_id = ?",
                            (payload.format_sample_id, user["id"]))
            if sample:
                fmt = docx_builder.merge_format(docx_builder.read_sample_format(Path(sample["path"])))
        out_path = config.FILES_DIR / user["id"] / f"{db.new_id('dl_')}.docx"
        docx_builder.build_docx(rebuilt, out_path, fmt)
        report = docx_builder.verify_docx(out_path, fmt, payload.word_count or 0, len(rebuilt["sections"]))
        file_row = workflow.register_deliverable(
            user["id"], task_id, out_path, rebuilt["title"] + ".docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document", report,
        )
        return {"ok": True, "file_id": file_row["id"], "report": report}

    result = await asyncio.to_thread(build)
    return result


@router.post("/{task_id}/approve")
async def approve_task(task_id: str, request: Request):
    """确认大纲，续跑 render → validate → deliver。纯本地渲染不扣额度；
    validate 失败触发内容重试时由执行链内部按口径补计 1 次。"""
    user, task = _own(request, task_id)
    if task["status"] != "waiting_approval":
        raise HTTPException(status_code=409, detail="任务不在待确认状态")
    capabilities.require("pptx.create")

    async def factory(emit):
        return await workflow.approve_ppt(user, task, emit)

    return StreamingResponse(_sse_stream(factory), media_type="text/event-stream", headers=_headers())


class ReviseIn(BaseModel):
    feedback: str


@router.post("/{task_id}/revise")
async def revise_task(task_id: str, payload: ReviseIn, request: Request):
    """按修改意见重跑 content（扣 1 次），下游节点作废，再次进入待确认。"""
    user, task = _own(request, task_id)
    if task["status"] != "waiting_approval":
        raise HTTPException(status_code=409, detail="任务不在待确认状态")
    feedback = (payload.feedback or "").strip()
    if not feedback:
        raise HTTPException(status_code=400, detail="请填写修改意见")
    deps.limit_chat(user["id"])
    if not config.MINIMAX_API_KEY:
        raise HTTPException(status_code=503, detail="服务端还没有配置模型 Key")

    call_id, reason = deps.reserve_call(user["id"], "retry")
    if not call_id:
        if reason == "running":
            raise HTTPException(status_code=409, detail="上一个生成还在跑，等它结束再发。")
        raise HTTPException(
            status_code=429,
            detail={
                "message": f"你今天 {deps.quota_for(user)} 次的额度用完了，明天自动恢复。",
                "code": "quota_exhausted",
                "quota": deps.quota_for(user),
                "used": deps.used_calls_today(user["id"]),
            },
        )

    async def factory(emit):
        try:
            result = await workflow.revise_ppt(user, task, feedback, emit)
        except BaseException:
            deps.settle_call(call_id, False)
            raise
        deps.settle_call(call_id, bool(result.get("ok")))
        return result

    return StreamingResponse(_sse_stream(factory), media_type="text/event-stream", headers=_headers())


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str, request: Request):
    """放弃当前大纲：任务回到可编辑状态，之后可以重新生成。"""
    user, task = _own(request, task_id)
    if task["status"] != "waiting_approval":
        raise HTTPException(status_code=409, detail="任务不在待确认状态")
    db.execute(
        "UPDATE tasks SET status = 'active', stage = 'intake', updated_at = ? WHERE id = ?",
        (db.now_iso(), task_id),
    )
    return {"ok": True}
