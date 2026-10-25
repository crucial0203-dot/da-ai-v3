"""流式对话。SSE 自己发，不经任何中间框架封装。

心跳用生产者-消费者结构：上游每吐一段就入队，消费者 8 秒取不到就发一行
注释保活，避免客户端或网关把长思考误判为断开。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import config, db, deps, security
from ..services import (
    artifacts as artifacts_service,
    chat_context,
    llm,
    materials as materials_service,
    memory as memory_service,
    models,
    prompts,
    skills_loader,
    tools as tools_service,
    vision,
)
from ..services.tools import fence_watch

router = APIRouter(prefix="/api/tasks", tags=["chat"])


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    # 两个都可空：老客户端只发 {text} 时行为完全不变
    model: str | None = None
    effort: str | None = None
    # V3.2 计划模式：只输出执行计划不执行；纯 bool 无需门禁
    plan_mode: bool = False
    # V4.0：任务级联网开关与知识库集合（不传则沿用任务 params）
    web_search: bool | None = None
    rag_collection_ids: list[str] | None = None
    tool_names: list[str] | None = None


def _task_params(task) -> dict:
    try:
        return json.loads(task["params_json"] or "{}")
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


def _attach_images(messages: list[dict], blocks: list[dict]) -> None:
    """把图片挂到对话的**第一条 user 消息**上，而不是本轮末尾。

    这样每一轮请求的前缀都是 `system + [user1(图+文), assistant1, ...]`，
    前缀稳定才能吃到 prompt 缓存（缓存读 $0.015/1M vs 全价 $0.15/1M）。
    挂在末尾的话每轮前缀都变，图片每轮都按全价计费。
    """
    for message in messages:
        if message.get("role") == "user":
            message["content"] = list(blocks) + list(message.get("content") or [])
            return
    messages.insert(0, {"role": "user", "content": list(blocks)})


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _error_severity(status: int | None) -> tuple[str, bool]:
    """(severity, retryable)——SSE error 事件的两级语义。

    429 是限流/配额：等一会就好，warn 不吓人，可以重试；
    5xx 是上游抽风：error 但值得重试；
    其余 4xx 与内部错误：重试大概率同样炸，不给重试按钮。
    fatal 留给「这一轮彻底没救」的场景（当前没有发射点，组件先支持）。
    """
    if status == 429:
        return "warn", True
    if status is not None and status >= 500:
        return "error", True
    return "error", False


def _approval_details(tool_input: dict) -> list[dict]:
    """把工具参数摊成「影响面」清单：label=参数名，value=值（截断 200 字）。

    审批卡要让用户在点「允许」之前看清这次调用到底会动什么——
    一坨 JSON 大多数人不会细看，逐字段一行才是给人看的。
    """
    out: list[dict] = []
    for key, value in (tool_input or {}).items():
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                text = str(value)
        if len(text) > 200:
            text = text[:200] + "…"
        out.append({"label": str(key), "value": text})
    return out


# 写操作类的审批走红底 destructive 变体：读/检索/抓取类审批只是看东西，
# 不值得用红色吓人；会改状态的才需要让用户停一秒。
_RISKY_KINDS = {"edit", "delete", "move", "switch_mode"}


def _approval_risk(name: str) -> str:
    return "high" if tools_service.kinds.kind_of(name) in _RISKY_KINDS else "normal"


def _persist_tool_exchange(task_id: str, call_text: str, result_text: str,
                           tool_name: str, model_id: str, usage: dict) -> None:
    """把一轮工具调用（模型吐的块 + 回注结果）落库。

    两条消息都写：
    - assistant 那条是模型的工具调用块（content 里含 ```tool 围栏）；
    - tool 那条是 `<<<TOOL_RESULT ...>>>` 回注块。

    回注用 role='tool' 而不是 'user'：它是工具执行结果，不是用户发言。
    前端据此不把它渲染成用户气泡，regenerate/continue 也不会把它当成
    用户提问（那会拿工具结果去重跑模型）。发给模型时仍在
    history_messages 里映射回 user 角色——文本协议没有原生 tool 通道。

    两条都标 status='done'，才能进下一轮的历史上下文——
    否则模型下一轮就「忘了」自己刚调过什么工具。
    """
    if not call_text.strip() and not result_text.strip():
        return
    now = db.now_iso()
    cols = ["task_id", "role", "content", "status", "created_at"]
    vals: list = [task_id, "assistant", call_text, "done", now]
    if db.has_column("messages", "model"):
        cols.append("model")
        vals.append(model_id)
    if db.has_column("messages", "tokens_in"):
        cols.extend(["tokens_in", "tokens_out", "cache_read"])
        vals.extend([
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
            int(usage.get("cache_read_input_tokens") or 0),
        ])
    with db.tx() as conn:
        conn.execute(
            f"INSERT INTO messages({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
            vals,
        )
        conn.execute(
            "INSERT INTO messages(task_id, role, content, status, created_at) VALUES(?, 'tool', ?, 'done', ?)",
            (task_id, result_text, now),
        )


def history_messages(task_id: str, keep_thinking: bool,
                     exclude_ids: tuple[int, ...] = ()) -> list[dict]:
    """取本任务的历史消息（给模型当上下文）。

    exclude_ids：要从历史里排除的消息 id。**本轮自己的 user 消息必须排掉**——
    它在调用本函数之前就已经落库了（status 默认 'done'），会被当历史读回来；
    调用方随后又把本轮消息拼一次，于是同一条用户消息发了两遍给模型：
    既多花 token，模型也可能把同一条指令理解成两次。
    """
    skip = set(exclude_ids)
    # 多取一条：排除掉本轮 user 消息后仍能凑满 8 条历史。
    limit = 8 + (1 if skip else 0)
    rows = db.rows(
        "SELECT id, role, content, thinking, signature FROM messages "
        "WHERE task_id = ? AND status = 'done' AND role != 'system' "
        "ORDER BY id DESC LIMIT ?",
        (task_id, limit),
    )
    rows = list(reversed(rows))
    messages: list[dict] = []
    assistant_seen = 0
    for row in rows:
        if row["id"] in skip:
            continue
        if row["role"] == "assistant":
            assistant_seen += 1
            include_thinking = keep_thinking and assistant_seen <= 2 and row["thinking"] and row["signature"]
            blocks: list[dict] = []
            if include_thinking:
                blocks.append({"type": "thinking", "thinking": row["thinking"], "signature": row["signature"]})
            blocks.append({"type": "text", "text": row["content"]})
            messages.append({"role": "assistant", "content": blocks})
        else:
            # role='tool' 是工具结果回注。文本协议没有原生 tool 通道，
            # 发给模型时仍按 user 角色送，靠 <<<TOOL_RESULT>>> 标记区分。
            messages.append({"role": "user", "content": [{"type": "text", "text": row["content"]}]})
    return messages


def _history_turns(messages: list[dict], reuse_message_id: int | None) -> int:
    """本轮之前的**对话轮次**数（上下文压力条的历史段）。

    口径说明：
      - 只数**用户真实发言**。工具结果回注（role='tool'，正文是
        <<<TOOL_RESULT>>> 块）是上一轮工具执行的产物，不是独立的一轮对话——
        按消息条数算的话，「用过一次工具」会显示成「历史 ×2」，用户在
        这个数字上判断上下文长度会被误导。
      - 历史上回注曾以 role='user' 落库，所以额外用正文标记兜底排除。
      - 本轮自己的 user 消息不计入（reuse_message_id 非空时它已经在
        messages 里，重生成不额外扣一次）。
    """
    turns = 0
    for message in messages:
        if message.get("role") != "user":
            continue
        parts = message.get("content")
        text = ""
        if isinstance(parts, list):
            text = "".join(
                p.get("text") or "" for p in parts if isinstance(p, dict)
            )
        elif isinstance(parts, str):
            text = parts
        if "<<<TOOL_RESULT" in text:
            continue
        turns += 1
    if reuse_message_id is None:
        # messages 末尾拼的是本轮 user 消息，它不是「此前」的历史
        turns = max(0, turns - 1)
    return turns


@router.post("/{task_id}/tool-approve")
async def tool_approve(task_id: str, request: Request):
    """工具审批：批准后执行工具并**续跑模型**；拒绝则回注「已拒绝」让模型降级。

    续跑是必须的：只落一条回注消息就返回，等于把模型停在了半截——
    用户批准完看到的仍是那句「需要批准」，看不到最终答复。
    """
    user = deps.current_user(request)
    task = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="无权访问这个任务")
    payload = await request.json()
    name = str(payload.get("name") or "")
    tool_input = payload.get("input") or {}
    approved = bool(payload.get("approved"))
    # 审批跨了 HTTP 请求，call_id 只能由前端带回来（SSE approval 事件里有）。
    # 老前端不回传时，退回落库的 tool_pending 里那份，保证回注和原工具卡对得上。
    call_id = str(payload.get("call_id") or "")
    if not call_id and db.has_column("tasks", "tool_pending"):
        try:
            stored = json.loads(task["tool_pending"] or "{}")
            if isinstance(stored, dict):
                call_id = str(stored.get("call_id") or "")
        except (KeyError, IndexError, json.JSONDecodeError, TypeError):
            call_id = ""
    if not name:
        raise HTTPException(status_code=400, detail="缺少工具名")

    if approved:
        tool_result = await tools_service.execute_tool(
            name, tool_input if isinstance(tool_input, dict) else {},
            {"user_id": user["id"], "task_id": task_id},
            approved=True,
        )
        payload_text = (
            tools_service.protocol.format_result(name, tool_result.output, call_id)
            if tool_result.ok
            else tools_service.protocol.format_error(name, tool_result.error, call_id)
        )
    else:
        payload_text = tools_service.protocol.format_error(
            name, "用户拒绝了这个工具的调用，请用现有信息回答，不要重试这个工具", call_id)

    with db.tx() as conn:
        conn.execute(
            "INSERT INTO messages(task_id, role, content, status, created_at) "
            "VALUES(?, 'tool', ?, 'done', ?)",
            (task_id, payload_text, db.now_iso()),
        )
        # 审批已处理，清掉待办标记
        if db.has_column("tasks", "tool_pending"):
            conn.execute("UPDATE tasks SET tool_pending = NULL WHERE id = ?", (task_id,))

    # 续跑：审批本身不额外扣额度——沿用本回合已预扣的那一次。
    resumed = ""
    try:
        resumed = await _resume_after_approval(user, task, name, payload_text)
    except Exception as exc:  # noqa: BLE001
        # 续跑失败不该让审批接口报错——回注已经落库，用户可以手动再发一条
        resumed = f"（续跑失败：{type(exc).__name__}）"

    return {"ok": True, "approved": approved, "name": name, "resumed": resumed}


async def _resume_after_approval(user, task, tool_name: str, payload_text: str) -> str:
    """审批后把模型叫回来继续答，返回它这一轮的正文。"""
    task_id = task["id"]
    resolved = models.resolve_model(None, _task_params(task), user)
    effort = models.normalize_effort(user.get("pref_effort")) or config.DEFAULT_EFFORT
    skills = skills_loader.active_prompt_skills(user["id"])
    materials, _ = materials_service.materials_text(task_id)
    task_params = _task_params(task)
    ctx = await chat_context.build_context_async(
        user_id=user["id"], user=user, task_params=task_params,
        query=payload_text[:200], tool_names=None,
    )
    system = chat_context.render_system(
        task["mode"], skills, materials,
        plan_mode=False, ctx=ctx,
    )
    messages = history_messages(task_id, bool(user["pref_keep_thinking"]))
    result: dict = {}

    async def run() -> str:
        text_parts: list[str] = []
        async for kind, chunk in llm.stream_chat(
            system, messages, effort=effort,
            max_tokens=config.MAX_TOKENS_CHAT, result=result,
            model=resolved.spec.id,
        ):
            if kind == "text":
                text_parts.append(chunk)
        return "".join(text_parts)

    try:
        text = await run()
    except llm.UpstreamError as exc:
        raise RuntimeError(exc.message) from exc

    now = db.now_iso()
    with db.tx() as conn:
        cols = ["task_id", "role", "content", "status", "created_at"]
        vals: list = [task_id, "assistant", text, "done", now]
        if db.has_column("messages", "model"):
            cols.append("model")
            vals.append(resolved.spec.id)
        usage = result.get("usage") or {}
        cols.extend(["tokens_in", "tokens_out", "cache_read"])
        vals.extend([
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
            int(usage.get("cache_read_input_tokens") or 0),
        ])
        conn.execute(
            f"INSERT INTO messages({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
            vals,
        )
    # 只记 token，不加次数：这一次调用属于原回合
    deps.record_usage(
        user["id"],
        int((result.get("usage") or {}).get("input_tokens") or 0),
        int((result.get("usage") or {}).get("output_tokens") or 0),
        int((result.get("usage") or {}).get("cache_read_input_tokens") or 0),
    )
    return text


@router.post("/{task_id}/chat")
async def chat(task_id: str, payload: ChatIn, request: Request):
    user = deps.current_user(request)
    task = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="无权访问这个任务")
    deps.limit_chat(user["id"])

    resolved = models.resolve_model(payload.model, _task_params(task), user)
    # key 就绪检查放在 resolve 之后：换模型场景下，缺的是目标 provider 的 Key
    try:
        llm.check_ready(resolved.spec.id)
    except llm.UpstreamError as exc:
        raise HTTPException(status_code=exc.status or 503, detail=exc.message)
    effort = (
        models.normalize_effort(payload.effort)
        or user["pref_effort"]
        or config.DEFAULT_EFFORT
    )

    # 图片门禁必须在预扣额度之前：让用户为一次注定失败的调用买单是不对的。
    # 而且纯文本模型收到 image block 不会报错，只会静默丢掉再拿文字硬猜，
    # 那种"看起来答了其实没看图"的结果比直接拒绝更糟。
    image_rows = vision.image_rows(task_id) if config.VISION_ENABLED else []
    if image_rows and not resolved.spec.vision:
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    f"当前模型「{resolved.spec.label}」看不了图片，这个任务里有 {len(image_rows)} 张图。"
                    f"先在输入框左下角切到 M3.1-Flash 或 M3，或者把图片删掉再发。"
                ),
                "code": "vision_unsupported",
                "model": resolved.spec.id,
            },
        )

    # 先预扣再开流：SSE 一旦开始写，就改不了 429 状态码了
    call_id, reason = deps.reserve_call(user["id"], "chat")
    if not call_id:
        if reason == "running":
            raise HTTPException(status_code=409, detail="上一条还在生成，等它结束再发。")
        quota = deps.quota_for(user)
        raise HTTPException(
            status_code=429,
            detail={
                "message": f"你今天 {quota} 次的额度用完了，明天自动恢复。",
                "code": "quota_exhausted",
                "quota": quota,
                "used": deps.used_calls_today(user["id"]),
            },
        )

    try:
        return await _chat_stream(user, task, payload, call_id,
                                  resolved, effort, image_rows)
    except HTTPException:
        deps.settle_call(call_id, False)
        raise
    except Exception:
        deps.settle_call(call_id, False)
        raise


@router.post("/{task_id}/regenerate")
async def regenerate(task_id: str, request: Request):
    """重新生成最后一条 AI 回复：旧回复标记 aborted（留审计），拿它前面那条 user 消息重跑。"""
    user = deps.current_user(request)
    task = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="无权访问这个任务")
    deps.limit_chat(user["id"])

    # 工具调用块也是 assistant 消息，但它不是「回复」——排除掉，
    # 否则最后一轮工具跑完没有总结时，重生成会把工具块当回复重跑。
    last = db.one(
        "SELECT * FROM messages WHERE task_id = ? AND role = 'assistant' AND status = 'done' "
        "AND content NOT LIKE '%```tool%' ORDER BY id DESC LIMIT 1", (task_id,),
    )
    if not last:
        raise HTTPException(status_code=404, detail="还没有可重新生成的回复")
    # 回注现在是 role='tool'，不会命中；老数据回注仍是 role='user'，
    # 用正文标记兜底排除，免得拿工具结果当用户提问重跑。
    user_row = db.one(
        "SELECT * FROM messages WHERE task_id = ? AND role = 'user' AND id < ? "
        "AND content NOT LIKE '%<<<TOOL_RESULT%' ORDER BY id DESC LIMIT 1",
        (task_id, last["id"]),
    )
    if not user_row:
        raise HTTPException(status_code=404, detail="找不到这条回复对应的提问")

    resolved = models.resolve_model(None, _task_params(task), user)
    try:
        llm.check_ready(resolved.spec.id)
    except llm.UpstreamError as exc:
        raise HTTPException(status_code=exc.status or 503, detail=exc.message)
    effort = user["pref_effort"] or config.DEFAULT_EFFORT
    image_rows = vision.image_rows(task_id) if config.VISION_ENABLED else []
    if image_rows and not resolved.spec.vision:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"当前模型「{resolved.spec.label}」看不了图片，先切到 M3.1-Flash 或 M3 再重新生成。",
                "code": "vision_unsupported",
                "model": resolved.spec.id,
            },
        )

    call_id, reason = deps.reserve_call(user["id"], "retry")
    if not call_id:
        if reason == "running":
            raise HTTPException(status_code=409, detail="上一条还在生成，等它结束再发。")
        quota = deps.quota_for(user)
        raise HTTPException(
            status_code=429,
            detail={
                "message": f"你今天 {quota} 次的额度用完了，明天自动恢复。",
                "code": "quota_exhausted",
                "quota": quota,
                "used": deps.used_calls_today(user["id"]),
            },
        )

    # 旧回复先标记：history_messages 只认 status='done'，它就不会再进上下文
    db.execute("UPDATE messages SET status = 'aborted' WHERE id = ?", (last["id"],))

    # P1-5 分支坐标：根一次回溯到根。last.parent_id 为空说明它是首版回复，
    # 根就是它自己；非空说明它已经是某条分支的产物，根是同一个——
    # 这样连点三次重生成，三条新消息的 parent_id 相同，前端才归得进同一组。
    # branch_index 取该根下已有子消息数（含已 aborted 的，它们仍占槽位供切换）。
    root_id = last["parent_id"] or last["id"]
    branch_index = 0
    if db.has_column("messages", "parent_id"):
        row = db.one("SELECT COUNT(*) AS n FROM messages WHERE parent_id = ?", (root_id,))
        branch_index = int(row["n"] or 0) if row else 0

    payload = ChatIn(text=user_row["content"])
    # 计划消息重跑仍是计划：优先读 user 那条的标记，缺失再退回 assistant 的
    # （列由迁移保证，缺失按非计划处理）。
    if db.has_column("messages", "plan_flag"):
        payload.plan_mode = bool(user_row["plan_flag"]) or bool(last["plan_flag"])
    try:
        return await _chat_stream(user, task, payload, call_id,
                                  resolved, effort, image_rows,
                                  reuse_message_id=user_row["id"],
                                  parent_id=root_id if db.has_column("messages", "parent_id") else None,
                                  branch_index=branch_index if db.has_column("messages", "branch_index") else None)
    except HTTPException:
        deps.settle_call(call_id, False)
        raise
    except Exception:
        deps.settle_call(call_id, False)
        raise


@router.post("/{task_id}/chat/continue")
async def continue_stopped(task_id: str, request: Request):
    """P1-4：接续一条被停止的半截回复（前缀续跑）。

    语义：把 stopped 消息的正文作为 assistant prefill 回注，模型从断点接着写；
    新消息入库 content = 半截正文 + 续跑文本，旧消息标记 aborted 让位
    （与重生成同一套规矩：离开上下文、留在审计里）。
    body {"discard": true} 则只标记丢弃，不调模型。

    顺序要紧：旧消息的让位标记放在 reserve_call **之后**——
    429/409 时用户那半截正文必须还在，不能被一次失败的续跑抹掉。
    """
    user = deps.current_user(request)
    task = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="无权访问这个任务")
    deps.limit_chat(user["id"])

    stopped_row = db.one(
        "SELECT * FROM messages WHERE task_id = ? AND role = 'assistant' AND status = 'stopped' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    )
    if not stopped_row:
        raise HTTPException(status_code=404, detail="没有被停止、可继续的回复")

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    def _retire_stopped() -> None:
        # 半截回复让位：history_messages 只认 done，它不会再进上下文
        if db.has_column("messages", "stopped"):
            db.execute(
                "UPDATE messages SET status = 'aborted', stopped = 0 WHERE id = ?",
                (stopped_row["id"],),
            )
        else:
            db.execute("UPDATE messages SET status = 'aborted' WHERE id = ?", (stopped_row["id"],))
        # 停止时可能还挂着待批准工具：那条流已经没了，审批卡留着永远等不到人
        if db.has_column("tasks", "tool_pending"):
            db.execute("UPDATE tasks SET tool_pending = NULL WHERE id = ?", (task_id,))

    if body.get("discard"):
        _retire_stopped()
        return {"ok": True, "discarded": True}

    prefix = stopped_row["content"] or ""
    # 续跑的检索 query 用它前面那条 user 消息：拿半截残句当 query 会搜出碎渣
    user_row = db.one(
        "SELECT * FROM messages WHERE task_id = ? AND role = 'user' AND id < ? "
        "AND content NOT LIKE '%<<<TOOL_RESULT%' ORDER BY id DESC LIMIT 1",
        (task_id, stopped_row["id"]),
    )
    if not user_row:
        raise HTTPException(status_code=404, detail="找不到这条回复对应的提问")

    resolved = models.resolve_model(None, _task_params(task), user)
    try:
        llm.check_ready(resolved.spec.id)
    except llm.UpstreamError as exc:
        raise HTTPException(status_code=exc.status or 503, detail=exc.message)
    effort = user["pref_effort"] or config.DEFAULT_EFFORT
    image_rows = vision.image_rows(task_id) if config.VISION_ENABLED else []
    if image_rows and not resolved.spec.vision:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"当前模型「{resolved.spec.label}」看不了图片，先切到 M3.1-Flash 或 M3 再继续。",
                "code": "vision_unsupported",
                "model": resolved.spec.id,
            },
        )

    call_id, reason = deps.reserve_call(user["id"], "retry")
    if not call_id:
        if reason == "running":
            raise HTTPException(status_code=409, detail="上一条还在生成，等它结束再发。")
        quota = deps.quota_for(user)
        raise HTTPException(
            status_code=429,
            detail={
                "message": f"你今天 {quota} 次的额度用完了，明天自动恢复。",
                "code": "quota_exhausted",
                "quota": quota,
                "used": deps.used_calls_today(user["id"]),
            },
        )

    _retire_stopped()

    # 分支槽位与旧消息对齐：续跑结果是旧分支的接班人，不是新开一支
    parent_id = stopped_row["parent_id"] or stopped_row["id"]
    branch_index = stopped_row["branch_index"] or 0

    payload = ChatIn(text=user_row["content"])
    # 计划消息续跑仍是计划（列由迁移保证，缺失按非计划处理）
    if db.has_column("messages", "plan_flag"):
        payload.plan_mode = bool(user_row["plan_flag"]) or bool(stopped_row["plan_flag"])
    try:
        return await _chat_stream(user, task, payload, call_id,
                                  resolved, effort, image_rows,
                                  reuse_message_id=user_row["id"],
                                  prefix=prefix,
                                  parent_id=parent_id if db.has_column("messages", "parent_id") else None,
                                  branch_index=branch_index if db.has_column("messages", "branch_index") else None)
    except HTTPException:
        deps.settle_call(call_id, False)
        raise
    except Exception:
        deps.settle_call(call_id, False)
        raise


# 后台任务引用池：asyncio 要求持有 Task 引用，否则它可能在运行途中被 GC。
# 记忆评审是 fire-and-forget，原先丢弃返回值，失效时又无任何日志记录。
_BACKGROUND_TASKS: set = set()


async def _guarded_stream(inner, call_id: str):
    """给 SSE 生成器兜一层结算护栏。

    生成器体是在 StreamingResponse 返回**之后**才惰性执行的，内部那条唯一的
    settle_call 一旦被异常绕过，`ai_calls` 就停在 reserved：quick requests
    会被判定为"上一次还没跑完"，该用户 600 秒内全部被拒，额度还显示已扣。
    这里按行状态兜底——只有仍是 reserved 才补一次失败结算；已经结算过就别动，
    免得把已计费的成功调用改写成 failed，造成 usage_daily 与实际不符。
    """
    try:
        async for chunk in inner:
            yield chunk
    finally:
        try:
            row = db.one("SELECT status FROM ai_calls WHERE request_id = ?", (call_id,))
            if row and str(row["status"]) == "reserved":
                deps.settle_call(call_id, False)
        except Exception:  # noqa: BLE001
            pass


async def _chat_stream(user, task, payload, call_id: str,
                       resolved, effort: str, image_rows: list,
                       reuse_message_id: int | None = None,
                       prefix: str = "",
                       parent_id: int | None = None,
                       branch_index: int | None = None):
    task_id = task["id"]
    text = payload.text.strip()
    if reuse_message_id is not None:
        # 重新生成：不重复落 user 消息，复用原文那条
        user_msg_id = reuse_message_id
    else:
        with db.tx() as conn:
            cols = ["task_id", "role", "content", "created_at"]
            vals: list = [task_id, "user", text, db.now_iso()]
            # 计划标记同时落在 user 与 assistant 上：
            # 重生成时要回读 user 那条（见 regenerate），只写 assistant 会取不到。
            if db.has_column("messages", "plan_flag"):
                cols.append("plan_flag")
                vals.append(1 if payload.plan_mode else 0)
            cursor = conn.execute(
                f"INSERT INTO messages({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                vals,
            )
            user_msg_id = cursor.lastrowid
            conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (db.now_iso(), task_id))

    # 读盘 + base64 是 CPU 活，丢到线程里做，别卡住事件循环
    blocks: list[dict] = []
    img_stats = {"sent": 0, "dropped": 0, "names": [], "b64_bytes": 0}
    if image_rows:
        blocks, img_stats = await asyncio.to_thread(vision.build_blocks, image_rows)

    materials, mats = materials_service.materials_text(task_id)
    # 注入集 = 全局启用的 prompt 技能 ∪ 本任务绑定的 prompt 技能。
    # 绑定技能实时取：停用后不再注入，不做正文快照（快照会绕过启停开关）。
    skills = skills_loader.active_prompt_skills(user["id"])
    task_params = _task_params(task)
    bound_ids = [s for s in (task_params.get("prompt_skill_ids") or []) if isinstance(s, str)]
    if bound_ids:
        global_ids = {s["id"] for s in skills}
        extra = skills_loader.skills_by_ids(user["id"], bound_ids)
        skills = skills + [s for s in extra if s["id"] not in global_ids]

    # V4.0 上下文装配：联网开关与知识库集合可以随请求覆盖任务的默认值
    effective_params = dict(task_params)
    if payload.web_search is not None:
        effective_params["web_search"] = bool(payload.web_search)
    elif "web_search" not in effective_params:
        # 默认按访客偏好；偏好列缺失时退化为「开」
        effective_params["web_search"] = bool(user.get("pref_web_search", 1))
    if payload.rag_collection_ids is not None:
        effective_params["rag_collection_ids"] = [
            c for c in payload.rag_collection_ids if isinstance(c, str)
        ]

    ctx = await chat_context.build_context_async(
        user_id=user["id"], user=user, task_params=effective_params,
        query=text, tool_names=payload.tool_names,
    )
    system = chat_context.render_system(
        task["mode"], skills, materials,
        plan_mode=bool(payload.plan_mode), ctx=ctx,
    )
    # 真实使用计数：这轮实际注入了谁，谁的 use_count +1
    db.bump_skill_use([s["id"] for s in skills])
    messages = history_messages(
        task_id, bool(user["pref_keep_thinking"]),
        # 本轮 user 消息已经落库了（见上面的 INSERT），要从历史里排掉，
        # 否则下面 append 时同一条消息会进上下文两次。
        exclude_ids=(user_msg_id,) if reuse_message_id is None else (),
    )
    first_round = not any(m.get("role") == "assistant" for m in messages)
    if reuse_message_id is None:
        messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
    else:
        # 重新生成：原 user 消息已经在 history 末尾，别再拼一遍
        if not messages or messages[-1].get("role") != "user":
            messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
    if blocks:
        _attach_images(messages, blocks)
        if first_round:
            # 只在第一轮说明有哪几张图；后续轮次图已在历史里，重复说明纯属浪费 token
            messages[-1]["content"][0]["text"] = vision.note_text(img_stats) + text
    if prefix:
        # P1-4 前缀续跑：把半截正文作为 assistant prefill 回注，模型从断点接着写。
        # 必须放在图片挂载之后——images[-1] 此时还得是 user 消息。
        messages.append({"role": "assistant", "content": [{"type": "text", "text": prefix}]})

    async def stream() -> AsyncIterator[str]:
        queue: asyncio.Queue = asyncio.Queue()
        result: dict = {}
        # 工具回路状态：一个用户回合只 reserve_call 一次，回路内的 LLM 只记账
        tool_events: list[dict] = []
        artifact_ids: list[str] = []
        loop_messages = list(messages)
        loop_system = system
        rounds = 0
        # 工具回路里的每一轮都是一次真实的上游调用，用量要累加——
        # 只记最后一轮的话，工具跑了 3 次模型只被记 1 次，账目对不上。
        usage_total = {"input_tokens": 0, "output_tokens": 0,
                       "cache_read_input_tokens": 0, "thinking_tokens": 0}

        def _accumulate_usage(source: dict) -> None:
            usage = source.get("usage") or {}
            for key in usage_total:
                try:
                    usage_total[key] += int(usage.get(key) or 0)
                except (TypeError, ValueError):
                    pass

        # ---- SSE tool 事件的公共字段 ----
        #
        # call_id：同一次工具调用的 running/waiting/done 三个状态共用一个 id，
        # 前端按它合并成一张卡。每次 running 生成新 id——同一个工具在一轮
        # 对话里可能被调多次（如两轮都调 web_search），按 name 合并会把两次
        # 调用叠成一张卡，第二次的结果覆盖第一次。
        # kind/label：语义分类与中文名，后端算（见 tools/kinds.py）。
        # ts/seq：合帧之后仍能还原真实顺序，前端断线重放时按 seq 排序。
        tool_seq = 0
        tool_call_ids: dict[str, str] = {}
        # P1-7：流式围栏阶段建过卡的工具名。用完即从集合里移除——
        # 同一个工具在同一轮里可能被调多次，只有**第一次**能复用流式卡，
        # 第二次要新开一张（否则两次调用叠成一张卡）。
        streaming_cards: set[str] = set()

        def _tool_event(name: str, status: str, **extra) -> str:
            nonlocal tool_seq
            tool_seq += 1
            if status == "running":
                # 同一个工具在一轮里可能被调多次（两轮都调 web_search），
                # 每次 running 都要新 call_id，否则两次调用会叠成一张卡。
                # 但流式围栏阶段已经给这次调用建过卡，那就复用——
                # 重复建卡会让用户以为调了两次。
                call_id = (tool_call_ids.get(name, "") if name in streaming_cards
                           else "") or f"c{tool_seq}"
                tool_call_ids[name] = call_id
            else:
                call_id = tool_call_ids.get(name) or f"c{tool_seq}"
            payload = {
                "name": name,
                "status": status,
                "call_id": call_id,
                "ts": round(time.time(), 3),
                "seq": tool_seq,
                **tools_service.kinds.tool_meta(name),
                **extra,
            }
            return sse("tool", payload)

        async def produce():
            try:
                async for kind, chunk in llm.stream_chat(
                    loop_system, loop_messages, effort=effort,
                    max_tokens=config.MAX_TOKENS_CHAT, result=result,
                    model=resolved.spec.id,
                ):
                    await queue.put((kind, chunk))
                await queue.put(("end", None))
            except Exception as exc:  # noqa: BLE001
                await queue.put(("err", exc))

        task_handle = asyncio.create_task(produce())
        meta_payload = {
            "task_id": task_id, "user_message_id": user_msg_id,
            "materials": len(mats), "skills": [s["name"] for s in skills],
            "effort": effort,
            "images": img_stats["sent"], "images_dropped": img_stats["dropped"],
            "rag": len(ctx.rag_hits), "web": len(ctx.web_sources),
            "web_provider": ctx.web_provider, "tools": ctx.enabled_tools,
            "memory": bool(ctx.memory_text),
            # P1-8 上下文压力条的六段计数：材料/技能/RAG/联网/记忆/历史。
            # 前五项上面已有（memory 原是布尔，这里补条数）。
            #
            # 历史的口径是**对话轮次**（此前用户消息的条数），不是消息条数：
            # 工具结果回注（<<<TOOL_RESULT>>>）也是一条 user 消息，但它是上一轮
            # 的产物、不是一次独立对话，按条数算会把「用了 1 次工具」显示成
            # 「历史 ×2」。用户看这个数字是为了判断上下文有多长，轮次更贴近。
            # 重生成时 user 消息已在 messages 里，不重复计。
            "memory_count": memory_service.count_memories(user["id"]) if ctx.memory_text else 0,
            "history": _history_turns(messages, reuse_message_id),
        }
        meta_payload.update(resolved.as_dict())
        yield sse("meta", meta_payload)
        if ctx.web_sources:
            yield sse("search", {"sources": ctx.web_sources, "provider": ctx.web_provider})
        if prefix:
            # 直播视图从头就是完整的：先把半截正文推一段，续跑文本随后到
            yield sse("text", {"t": prefix})
        timed_out = False
        aborted = False
        errored = False
        pending_approval: dict | None = None
        # P1-7：流式围栏观察用的累加器。stream_text 是本轮上游已吐出的
        # 全文（工具围栏从中找）；last_chars 是上次上报的字符数（避免重复发）。
        stream_text = prefix or ""
        last_chars = 0
        try:
            while True:
                try:
                    kind, chunk = await asyncio.wait_for(queue.get(), timeout=8.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    timed_out = True
                    continue
                if kind == "end":
                    break
                if kind == "err":
                    exc = chunk
                    errored = True
                    if isinstance(exc, llm.UpstreamError):
                        severity, retryable = _error_severity(exc.status)
                        yield sse("error", {"message": exc.message, "status": exc.status,
                                            "severity": severity, "retryable": retryable})
                    else:
                        yield sse("error", {"message": f"调用失败：{type(exc).__name__}",
                                            "severity": "error", "retryable": False})
                    break
                if kind == "text":
                    # P1-7：边流边看工具围栏。围栏一开就先发一张「正在写」的卡，
                    # 之后每收到新字符就更新一次已写出的字符数。
                    #
                    # 原来后端是整轮收完文本才解析围栏，工具卡的 running 事件
                    # 等围栏闭合才发——从模型开始写参数到卡片出现之间有一段
                    # 空窗，制品卡的「正在写 + 实时字数」正好落在空窗里。
                    #
                    # 参考实现：assistant-ui 的 ToolCallMessagePart 把工具参数
                    # 定义成流式部分解析的 JSON，useToolArgsStatus 判断哪个
                    # 字段还在写。这里是同构的轻量版。
                    stream_text += chunk
                    fence = fence_watch.observe(stream_text)
                    if fence.open:
                        fence_name = fence.tool_name or ""
                        if fence_name and fence_name not in tool_call_ids:
                            # 围栏刚开：先发一次 running，让前端把卡挂出来。
                            # call_id 用 s 前缀（streaming）标记这是流式阶段建的卡，
                            # 工具回路后面会复用它而不是新建一张。
                            tool_call_ids[fence_name] = f"s{tool_seq + 1}"
                            streaming_cards.add(fence_name)
                            yield _tool_event(fence_name, "running",
                                              chars=fence.chars,
                                              streaming_args=True,
                                              input=fence.input_preview)
                            tool_seq += 1
                            last_chars = fence.chars
                        else:
                            # 围栏还在写：更新字符数 + 当前已写出的参数
                            # （fix-json 补全后的部分字段，见 fence_watch），
                            # 不新建卡
                            if fence_name and fence.chars > last_chars:
                                last_chars = fence.chars
                                yield sse("tool", {
                                    "name": fence_name,
                                    "status": "running",
                                    "call_id": tool_call_ids.get(fence_name, ""),
                                    "chars": fence.chars,
                                    "streaming_args": True,
                                    "input": fence.input_preview,
                                })
                    else:
                        last_chars = 0
                yield sse(kind, {"t": chunk})

            _accumulate_usage(result)

            # ---- V4.0 工具回路 ----
            #
            # 模型吐 ```tool {...} ``` → 白名单强制 → 审批闸 → 执行 →
            # 结果作为新 user 消息回注 → 再调模型。最多 MAX_TOOL_ROUNDS 轮。
            if (not errored and not aborted and config.TOOLS_ENABLED
                    and ctx.enabled_tools and result.get("text")):
                rounds = 0
                while rounds < config.MAX_TOOL_ROUNDS:
                    calls, parse_errors = tools_service.parse_tool_calls(result.get("text") or "")
                    if parse_errors:
                        yield _tool_event("_parse", "error",
                                          error="；".join(parse_errors))
                    if not calls:
                        break
                    call = calls[0]
                    if call.name not in ctx.enabled_tools:
                        # input 一起带上：被拒的调用往往是要复现的问题现场，
                        # 只给一句「未启用」用户和模型都不知道模型想干什么。
                        yield _tool_event(call.name, "error",
                                          input=call.input,
                                          error=f"工具 {call.name} 未启用或不在白名单内")
                        break
                    # P1-7：围栏观察阶段（流式）可能已经为这个工具发过
                    # 一张「正在写」的卡（带 streaming_args=True）。
                    # 那时不能新建卡，否则同一张工具卡会出现两张——
                    # 这里复用围栏阶段的 call_id，只把完整的 input 补上去。
                    if call.name in streaming_cards:
                        streaming_cards.discard(call.name)
                        yield sse("tool", {
                            "name": call.name,
                            "status": "running",
                            "call_id": tool_call_ids.get(call.name, ""),
                            "input": call.input,
                        })
                    else:
                        yield _tool_event(call.name, "running", input=call.input)
                    tool_result = await tools_service.execute_tool(
                        call.name, call.input,
                        {"user_id": user["id"], "task_id": task_id},
                        perms=effective_params.get("permissions") or {},
                    )
                    if tool_result.needs_approval:
                        pending_approval = {
                            "name": call.name, "input": call.input,
                            "call_id": tool_call_ids.get(call.name, ""),
                            "reason": tool_result.meta.get("reason", ""),
                        }
                        # 落到任务上：刷新页面后前端还能看到「有个工具在等你批准」
                        if db.has_column("tasks", "tool_pending"):
                            db.execute(
                                "UPDATE tasks SET tool_pending = ? WHERE id = ?",
                                (json.dumps(pending_approval, ensure_ascii=False), task_id),
                            )
                        yield sse("approval", {
                            "name": call.name,
                            "call_id": tool_call_ids.get(call.name, ""),
                            "label": tools_service.kinds.label_of(call.name),
                            "kind": tools_service.kinds.kind_of(call.name),
                            "risk": _approval_risk(call.name),
                            "input": call.input,
                            "details": _approval_details(call.input),
                            "reason": tool_result.meta.get("reason", ""),
                            "task_id": task_id,
                        })
                        yield _tool_event(call.name, "waiting")
                        break
                    # 制品工具：记下 id，done 时提示前端
                    if call.name == "artifact_create":
                        out = tool_result.output
                        if isinstance(out, dict) and out.get("artifact_id"):
                            artifact_ids.append(out["artifact_id"])
                    yield _tool_event(call.name, "done" if tool_result.ok else "error",
                                      error=tool_result.error)
                    tool_events.append({
                        "name": call.name,
                        "ok": tool_result.ok,
                        "error": tool_result.error,
                    })
                    result_call_id = tool_call_ids.get(call.name, "")
                    if not tool_result.ok:
                        # 失败也回注，让模型自己降级
                        payload_text = tools_service.protocol.format_error(
                            call.name, tool_result.error, result_call_id)
                    else:
                        payload_text = tools_service.protocol.format_result(
                            call.name, tool_result.output, result_call_id)
                    # 工具调用块与回注都要落库：
                    # 不落的话刷新页面只剩最终答复，用户看不到「调了什么、拿到了什么」，
                    # 也没法在下一轮把工具结果当作上下文继续用。
                    _persist_tool_exchange(
                        task_id, call_text=result.get("text") or "",
                        result_text=payload_text,
                        tool_name=call.name,
                        model_id=resolved.spec.id,
                        usage=result.get("usage") or {},
                    )
                    # 回注给模型的消息必须成对：assistant 的调用块 + user 的工具结果。
                    # 只拼结果会让上下文出现「连续两条 user 消息」，模型分不清
                    # 哪条是自己的调用、哪条是执行结果，也解释了「工具结果裸奔」。
                    loop_messages = list(loop_messages) + [
                        {"role": "assistant",
                         "content": [{"type": "text", "text": result.get("text") or ""}]},
                        {"role": "user", "content": [{"type": "text", "text": payload_text}]},
                    ]
                    rounds += 1
                    if rounds >= config.MAX_TOOL_ROUNDS:
                        # 最后一轮强制纯文本总结
                        loop_system = (
                            loop_system
                            + "\n\n现在直接给出最终答复的正文，不要再调用任何工具。"
                        )
                    # 重新调模型
                    result = {}
                    task_handle = asyncio.create_task(produce())
                    while True:
                        try:
                            kind, chunk = await asyncio.wait_for(queue.get(), timeout=8.0)
                        except asyncio.TimeoutError:
                            yield ": ping\n\n"
                            timed_out = True
                            continue
                        if kind == "end":
                            break
                        if kind == "err":
                            exc = chunk
                            errored = True
                            if isinstance(exc, llm.UpstreamError):
                                severity, retryable = _error_severity(exc.status)
                                yield sse("error", {"message": exc.message, "status": exc.status,
                                                    "severity": severity, "retryable": retryable})
                            else:
                                yield sse("error", {"message": f"调用失败：{type(exc).__name__}",
                                                    "severity": "error", "retryable": False})
                            break
                        yield sse(kind, {"t": chunk})
                    _accumulate_usage(result)
                    if errored:
                        break
        except (asyncio.CancelledError, GeneratorExit):
            aborted = True
        finally:
            if not task_handle.done():
                task_handle.cancel()

        # 用量取累加值：工具回路里的每一轮都是真实的上游调用
        usage = usage_total or (result.get("usage") or {})
        tokens_in = int(usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("output_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        # tail 是本次新产出的部分；有前缀时入库 content = 半截正文 + 续跑文本
        tail = result.get("text") or ""
        content = prefix + tail if prefix else tail
        thinking = result.get("thinking") or ""
        # 报错或中途挂断都不算成功：即便已经收到半截正文，也不该让用户为这次失败买单。
        # P1-4：用户主动停止**且拿到了半截正文** → status='stopped'（可继续、可丢弃）；
        # 空内容的停止、上游报错、客户端断线都仍是 'aborted'。
        # 断线与手动停止同走 CancelledError，无法在服务端区分——一律保留半截正文，
        # 用户回来后可继续也可丢弃，总比正文丢了强。
        succeeded = bool(tail) and not aborted and not errored
        stopped = bool(aborted and tail.strip())
        status = "stopped" if stopped else ("done" if succeeded else "aborted")

        with db.tx() as conn:
            cols = ["task_id", "role", "content", "thinking", "signature", "stop_reason",
                    "tokens_in", "tokens_out", "cache_read", "status", "created_at"]
            vals = [task_id, "assistant", content, thinking, result.get("signature") or "",
                    result.get("stop_reason"), tokens_in, tokens_out, cache_read, status,
                    db.now_iso()]
            # 迁移没跑成时这一列不存在，退化成不记模型名，不影响对话本身
            if db.has_column("messages", "model"):
                cols.append("model")
                vals.append(resolved.spec.id)
            if db.has_column("messages", "plan_flag"):
                cols.append("plan_flag")
                vals.append(1 if payload.plan_mode else 0)
            if db.has_column("messages", "skills"):
                cols.append("skills")
                vals.append(",".join(s["id"] for s in skills))
            if db.has_column("messages", "web_flag"):
                cols.append("web_flag")
                vals.append(1 if ctx.web_sources else 0)
            if db.has_column("messages", "web_sources"):
                cols.append("web_sources")
                vals.append(json.dumps(ctx.web_sources, ensure_ascii=False)[:8000])
            # P1-4/P1-5：stopped 标记与分支坐标。列缺失时静默跳过——
            # 老库迁移失败也只是少个「继续」按钮，对话照常。
            if db.has_column("messages", "stopped"):
                cols.append("stopped")
                vals.append(1 if stopped else 0)
            if db.has_column("messages", "parent_id"):
                cols.append("parent_id")
                vals.append(parent_id)
            if db.has_column("messages", "branch_index"):
                cols.append("branch_index")
                vals.append(branch_index)
            conn.execute(
                f"INSERT INTO messages({', '.join(cols)}) "
                f"VALUES({', '.join('?' * len(cols))})",
                vals,
            )
            conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (db.now_iso(), task_id))
        deps.settle_call(call_id, succeeded, tokens_in, tokens_out, cache_read)

        # ---- V4.0 记忆：正文内联块（同步）+ 回合后评审（异步）----
        #
        # 内联块必须先于 done 处理：模型有时不经过评审就直接在回答里写
        # ```memory，那是要立刻生效的；结果还要进 done payload，前端
        # 拿它在对话里渲染 memory chips（新增/更新/已存在）。
        # 评审保持回合后异步——它要多打一次模型，不能拖慢本次响应。
        memory_ops: list[dict] = []
        if succeeded and config.MEMORY_ENABLED and content.strip():
            inline_ops = memory_service.parse_ops(content)
            if inline_ops:
                try:
                    memory_ops = memory_service.apply_ops(user["id"], inline_ops, task_id)
                except Exception:
                    pass

        if succeeded:
            yield sse("usage", {
                "in": tokens_in, "out": tokens_out, "cache_read": cache_read,
                "thinking_tokens": usage.get("thinking_tokens", 0),
                "kept_awake": timed_out,
                "images": img_stats["sent"],
            })
            # 上游回显的模型名优先：它才是真正跑的那一个，用来证明"确实换了模型"
            done_payload = {
                "status": "done",
                "model": result.get("model") or resolved.spec.id,
                "requested_model": resolved.spec.id,
            }
            if tool_events:
                done_payload["tools"] = tool_events
            if artifact_ids:
                done_payload["artifacts"] = artifact_ids
            if ctx.web_sources:
                done_payload["web_sources"] = ctx.web_sources
            if pending_approval:
                done_payload["approval"] = pending_approval
            if memory_ops:
                done_payload["memory_ops"] = memory_ops
            yield sse("done", done_payload)
        elif not aborted and not errored:
            # 已经报过上游错误就不要再补一条"没有返回正文"，否则前端弹两个 toast
            yield sse("error", {
                "message": f"上游没有返回正文（stop_reason={result.get('stop_reason') or '未知'}），可以重试一次",
                "severity": "warn",
                "retryable": True,
            })

        # 回合后评审：失败静默——记忆是锦上添花，不该拖垮对话。
        # 结果只落 memories.change 列（记忆页徽标），不进本次 SSE——
        # 响应已经发了，评审那一次模型调用什么时候回来不确定。
    # ---- 后台任务句柄：create_task 不持引用会被 GC 中途回收 ----
        if succeeded and config.MEMORY_ENABLED and content.strip():
            task_handle_bg = asyncio.create_task(
                _review_memory(user["id"], content, task_id, resolved.spec.id, effort)
            )
            # create_task 的返回值必须持有：不被引用的 Task 可能在执行途中被 GC
            _BACKGROUND_TASKS.add(task_handle_bg)
            task_handle_bg.add_done_callback(_BACKGROUND_TASKS.discard)

    return StreamingResponse(
        _guarded_stream(stream(), call_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _review_memory(user_id: str, reply_text: str, task_id: str,
                         model_id: str, effort: str) -> None:
    """回合后评审记忆。失败静默——记忆是锦上添花，不该拖垮对话。"""
    try:
        reply = await llm.complete(
            "你是记忆评审员。只输出记忆操作指令，不要解释。",
            [{"role": "user", "content": [{"type": "text", "text": memory_service.review_prompt(reply_text)}]}],
            model=model_id, effort="low", max_tokens=800,
        )
        ops = memory_service.parse_ops(reply.get("text") or "")
        if ops:
            memory_service.apply_ops(user_id, ops, task_id)
    except Exception:
        # 评审失败不影响主流程
        pass
