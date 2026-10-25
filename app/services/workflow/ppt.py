"""PPT 六节点工作流执行器：plan → research → content → render → validate → deliver。

与旧 slides 链路的差别：content 完成后**暂停等待人工确认大纲**（渲染前确认），
确认后续跑 render → validate → deliver（纯本地渲染，不扣额度）；
提出修改意见则重跑 content（扣 1 次），下游节点整体作废（invalidate_downstream）。

节点定义来自任务快照绑定技能的 skill.json（meta.nodes）；
没有绑定技能时用内置 PPT_WORKFLOW_META，六节点顺序固定。

画布侧的同构定义在 builtin.py（ppt_workflow_graph），供 workflow_runs 引擎使用；
这里服务的是任务页的六节点链（task_nodes 表 + SSE），两者共享节点语义。
"""
from __future__ import annotations

import json
from pathlib import Path

from ... import config, db, deps
from .. import llm, models, pptx_builder, prompts, skill_tools, skills_loader
from ..materials import materials_text
from .task_nodes import (
    Emit,
    complete_json,
    ensure_workflow_nodes,
    fail_node,
    invalidate_downstream,
    node_get,
    node_out,
    node_set,
    params_of,
    record_usage,
    register_deliverable,
    reset_nodes,
    save_node_out,
)

VALID_LAYOUTS = {"cover", "section", "bullets", "two_col", "quote", "chart", "end"}

CONTENT_SCHEMA_NOTE = (
    "只输出一个 JSON 对象，格式：\n"
    '{"title": "演示标题", "subtitle": "副标题", "slides": ['
    '{"layout": "cover|bullets|two_col|quote|chart|section|end", '
    '"title": "页标题（≤18字）", "subtitle": "", '
    '"bullets": ["要点，每条≤40字"], "notes": "讲稿 60-100 字"}]}\n'
    "约束：每页必须有 title 与 notes；内容页必须有 bullets；"
    "数字必须来自材料原文，写不出出处就删掉数字；"
    "讲稿写进 notes，不要堆在页面上。"
)


def load_flow(task) -> tuple[dict, dict]:
    """返回 (流程 meta, 技能快照)。快照缺失或版本无法解析时退回内置流程。"""
    params = params_of(task)
    snap = params.get("skill") or {}
    meta: dict = {}
    if isinstance(snap, dict) and snap.get("id") and snap.get("version"):
        # 版本快照可解析是硬承诺：任务绑定 1.0 就永远按 1.0 跑，即使该版本已归档
        row = db.one(
            "SELECT meta_json FROM skill_versions WHERE skill_id = ? AND version = ?",
            (snap["id"], snap["version"]),
        )
        if row:
            try:
                loaded = json.loads(row["meta_json"] or "{}")
            except json.JSONDecodeError:
                loaded = {}
            if isinstance(loaded, dict) and not skill_tools.validate_workflow_config(loaded):
                meta = loaded
    if not meta:
        meta = dict(db.PPT_WORKFLOW_META)
    return meta, (snap if isinstance(snap, dict) else {})


def _labels(meta: dict) -> dict[str, str]:
    return {n["key"]: n.get("label") or n["key"] for n in meta.get("nodes") or []}


async def _emit_node(emit: Emit, task_id: str, labels: dict, key: str, status: str, **extra) -> None:
    payload = {"key": key, "label": labels.get(key, key), "status": status}
    payload.update(extra)
    await emit("node", payload)


def validate_slides(structure: dict, page_count: int | None = None) -> list[str]:
    """slides.json 两级校验：一级结构（页对象/版式/标题），二级内容（要点/讲稿）。"""
    errors: list[str] = []
    if not isinstance(structure, dict):
        return ["输出不是 JSON 对象"]
    if not (structure.get("title") or "").strip():
        errors.append("缺少演示标题 title")
    slides = structure.get("slides")
    if not isinstance(slides, list) or not slides:
        errors.append("slides 必须是非空数组")
        return errors
    for index, slide in enumerate(slides, 1):
        if not isinstance(slide, dict):
            errors.append(f"第 {index} 页不是对象")
            continue
        layout = slide.get("layout") or "bullets"
        if layout not in VALID_LAYOUTS:
            errors.append(f"第 {index} 页 layout 未知：{layout}")
        if not (slide.get("title") or "").strip():
            errors.append(f"第 {index} 页缺少标题 title")
        if layout in {"bullets", "two_col"} and not (slide.get("bullets") or []):
            errors.append(f"第 {index} 页缺少要点 bullets")
        if not (slide.get("notes") or "").strip():
            errors.append(f"第 {index} 页缺少讲稿 notes")
    if page_count and abs(len(slides) - page_count) > 2:
        errors.append(f"页数 {len(slides)} 与目标 {page_count} 页相差过大")
    return errors


def _content_prompt(goal: str, page_count: int, plan: dict, materials_hint: str,
                    extra: str = "") -> str:
    chapters = plan.get("chapters") or []
    plan_text = "；".join(
        f"{c.get('heading') or ''}（{c.get('question') or ''}）" for c in chapters if isinstance(c, dict)
    )
    return (
        f"汇报目标：{goal}\n"
        f"页数：约 {page_count} 页（含封面与结束页）\n"
        f"章节规划：{plan_text or '自行规划三段式结构'}\n"
        f"{materials_hint}\n"
        f"{extra}\n"
        f"{CONTENT_SCHEMA_NOTE}"
    )


async def _content_once(system: str, prompt: str, user_id: str, effort: str,
                        model_id: str) -> dict:
    reply = await llm.complete(
        system, [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        effort=effort, max_tokens=6000, model=model_id,
    )
    record_usage(user_id, reply)
    return llm.extract_json(reply["text"])


def _touch(task_id: str, status: str, stage: str) -> None:
    db.execute(
        "UPDATE tasks SET status = ?, stage = ?, updated_at = ? WHERE id = ?",
        (status, stage, db.now_iso(), task_id),
    )


async def generate_ppt(user, task, payload: dict, emit: Emit) -> dict:
    """generate 入口：跑 plan → research → content，然后停下等确认。"""
    task_id = task["id"]
    meta, snap = load_flow(task)
    labels = _labels(meta)
    resolved = models.resolve_model(payload.get("model"), params_of(task), user)
    model_id = resolved.spec.id
    effort = models.normalize_effort(payload.get("effort")) or user["pref_effort"] or config.DEFAULT_EFFORT
    goal = payload.get("goal") or task["goal"]
    schema = (meta.get("params_schema") or {}).get("page_count") or {}
    page_count = int(payload.get("page_count") or schema.get("default") or 10)

    ensure_workflow_nodes(task_id, meta)
    reset_nodes(task_id, meta)
    materials, mats = materials_text(task_id)
    system = prompts.build_system("slides", skills_loader.active_prompt_skills(user["id"]), materials)

    # plan：拆纲
    await _emit_node(emit, task_id, labels, "plan", "running")
    node_set(task_id, "plan", "running", started_at=db.now_iso())
    plan_prompt = (
        f"任务目标：{goal}\n页数：约 {page_count} 页（含封面与结束页）。\n"
        "请输出拆纲 JSON：{\"title\": \"演示标题\", \"audience\": \"受众与场合\", "
        "\"chapters\": [{\"heading\": \"章节名\", \"question\": \"本章要回答的问题\"}]}。\n"
        "章节页数量 = 页数 - 2，三段式结构；每章给一句「本章要回答的问题」，"
        "防止内容页写成名词罗列。"
    )
    try:
        plan = await complete_json(system, plan_prompt, user["id"], effort, 2500, model=model_id)
        if not isinstance(plan.get("chapters"), list) or not plan["chapters"]:
            raise ValueError("模型没有给出章节规划")
    except Exception as exc:
        await fail_node(emit, task_id, "plan", f"拆纲失败：{exc}")
        _touch(task_id, "failed", "plan")
        return {"ok": False, "error": f"拆纲失败：{exc}"}
    save_node_out(task_id, "plan", plan)
    await _emit_node(emit, task_id, labels, "plan", "done",
                     detail=f"{len(plan['chapters'])} 章 · {plan.get('title', '')}")

    # research：材料覆盖情况（抽取在上传时已完成，这里只登记与提示）
    await _emit_node(emit, task_id, labels, "research", "running")
    usable = [m for m in mats if m.get("chars")]
    if usable:
        total = sum(m.get("chars") or 0 for m in mats)
        save_node_out(task_id, "research", {"materials": mats, "chars": total})
        await _emit_node(emit, task_id, labels, "research", "done",
                         detail=f"{len(usable)} 份可用材料 · {total} 字")
    else:
        save_node_out(task_id, "research", {"materials": [], "skipped": True})
        await _emit_node(emit, task_id, labels, "research", "done", detail="无材料，按常识组织")

    # content：逐页内容，两级校验，失败自动重试（同一次额度内）
    await _emit_node(emit, task_id, labels, "content", "running")
    node_set(task_id, "content", "running", started_at=db.now_iso())
    materials_hint = (
        f"可用材料 {len(usable)} 份：数字与事实必须出自材料；"
        if usable else "没有上传材料：不要编造具体数字与引文；"
    )
    node = next((n for n in meta["nodes"] if n["key"] == "content"), {})
    retries = int(node.get("retries", 2))
    structure: dict = {}
    last_errors: list[str] = []
    prompt = _content_prompt(goal, page_count, plan, materials_hint)
    for attempt in range(retries + 1):
        try:
            candidate = await _content_once(system, prompt, user["id"], effort, model_id)
        except Exception as exc:
            last_errors = [f"模型调用失败：{exc}"]
            break
        last_errors = validate_slides(candidate, page_count)
        if not last_errors:
            structure = candidate
            break
        if attempt < retries:
            prompt = _content_prompt(
                goal, page_count, plan, materials_hint,
                extra="上一版输出存在以下问题，请修正后重新输出完整 JSON：\n- " + "\n- ".join(last_errors),
            )
    if not structure:
        message = "；".join(last_errors) or "未得到有效内容"
        await fail_node(emit, task_id, "content", f"内容校验未通过：{message}")
        _touch(task_id, "failed", "content")
        return {"ok": False, "error": f"内容校验未通过：{message}"}

    save_node_out(task_id, "content", structure)
    node_set(task_id, "content", "done", ended_at=db.now_iso())
    await _emit_node(emit, task_id, labels, "content", "done", detail=f"{len(structure['slides'])} 页")

    # 渲染前人工确认：任务挂起，等 approve / revise / cancel
    _touch(task_id, "waiting_approval", "approval")
    await emit("approval", _approval_payload(structure, snap))
    return {"ok": True, "waiting_approval": True, "slides": len(structure["slides"])}


def _approval_payload(structure: dict, snap: dict, revised: bool = False) -> dict:
    payload = {
        "title": structure.get("title", ""),
        "subtitle": structure.get("subtitle", ""),
        "page_count": len(structure.get("slides") or []),
        "slides": [
            {"index": i, "title": s.get("title", ""), "layout": s.get("layout") or "bullets"}
            for i, s in enumerate(structure.get("slides") or [], 1)
        ],
        "skill": {"id": snap.get("id"), "version": snap.get("version")} if snap else None,
    }
    if revised:
        payload["revised"] = True
    return payload


async def _render_and_verify(user, task, structure: dict, emit: Emit, labels: dict) -> tuple[dict, Path]:
    task_id = task["id"]
    params = params_of(task)
    node_set(task_id, "render", "running", started_at=db.now_iso())
    await _emit_node(emit, task_id, labels, "render", "running")
    template_path = None
    if params.get("template_id"):
        row = db.one("SELECT * FROM files WHERE id = ? AND user_id = ?",
                     (params["template_id"], user["id"]))
        if row:
            template_path = Path(row["path"])
    out_path = config.FILES_DIR / user["id"] / f"{db.new_id('dl_')}.pptx"
    info = pptx_builder.build_pptx(structure, out_path, template_path)
    report = pptx_builder.verify_pptx(out_path, len(structure["slides"]))
    report["mode"] = info.get("mode")
    save_node_out(task_id, "render", {"file_id": "", "mode": info.get("mode"), "path": str(out_path)})
    await _emit_node(emit, task_id, labels, "render", "done",
                     detail=f"{len(structure['slides'])} 页 · {info.get('mode')}")
    return report, out_path


async def approve(user, task, emit: Emit) -> dict:
    """确认大纲后续跑：render → validate → deliver。纯本地渲染不扣额度；
    validate 失败触发内容重试时按口径补计 1 次。"""
    task_id = task["id"]
    if task["status"] != "waiting_approval":
        return {"ok": False, "error": "当前不在待确认状态"}
    meta, snap = load_flow(task)
    labels = _labels(meta)
    structure = node_out(task_id, "content")
    if not structure.get("slides"):
        await fail_node(emit, task_id, "content", "待确认内容缺失，请重新生成")
        _touch(task_id, "failed", "content")
        return {"ok": False, "error": "待确认内容缺失，请重新生成"}

    _touch(task_id, "running", "render")
    try:
        report, out_path = await _render_and_verify(user, task, structure, emit, labels)
    except Exception as exc:
        await fail_node(emit, task_id, "render", f"PPT 生成失败：{exc}")
        _touch(task_id, "failed", "render")
        return {"ok": False, "error": f"PPT 生成失败：{exc}"}

    # validate
    await _emit_node(emit, task_id, labels, "validate", "running")
    node_set(task_id, "validate", "running", started_at=db.now_iso())
    retried = False
    if not report["ok"]:
        policy = meta.get("policy") or {}
        if policy.get("on_validate_fail") == "retry_content":
            # 口径：validate 失败触发内容重试才计 1 次——lazy 预扣，扣不到就如实报
            call_id, reason = deps.reserve_call(user["id"], "retry")
            if not call_id:
                message = "校验未通过且今日额度已用完，无法自动重试内容。"
                await fail_node(emit, task_id, "validate", message)
                _touch(task_id, "failed", "validate")
                return {"ok": False, "error": message, "report": report}
            try:
                fixed = await _retry_content(user, task, meta, emit, [c["detail"] for c in report["checks"] if not c["ok"]])
                deps.settle_call(call_id, True)
                retried = True
            except Exception as exc:
                deps.settle_call(call_id, False)
                await fail_node(emit, task_id, "validate", f"内容重试失败：{exc}")
                _touch(task_id, "failed", "validate")
                return {"ok": False, "error": f"内容重试失败：{exc}", "report": report}
            structure = fixed
            invalidate_downstream(task_id, "content")
            node_set(task_id, "render", "done", ended_at=db.now_iso())
            try:
                report, out_path = await _render_and_verify(user, task, structure, emit, labels)
            except Exception as exc:
                await fail_node(emit, task_id, "render", f"重渲染失败：{exc}")
                _touch(task_id, "failed", "render")
                return {"ok": False, "error": f"重渲染失败：{exc}"}
            if retried:
                await _emit_node(emit, task_id, labels, "validate", "running", detail="内容已按校验意见重写")

    node_set(task_id, "validate", "done" if report["ok"] else "failed",
             acceptance=json.dumps(report, ensure_ascii=False),
             error=None if report["ok"] else f"{report['total'] - report['passed']} 项待确认",
             ended_at=db.now_iso())
    await _emit_node(emit, task_id, labels, "validate", "done" if report["ok"] else "failed",
                     detail=f"通过 {report['passed']}/{report['total']}"
                            + ("（内容已重写一次）" if retried else ""))
    if not report["ok"]:
        # 校验不过不交付：用户可以提出修改（revise）或重新生成
        _touch(task_id, "failed", "validate")
        return {"ok": False, "error": "校验未通过，文件没有交付", "report": report}

    # deliver
    file_row = register_deliverable(
        user["id"], task_id, out_path, (structure.get("title") or task["title"]) + ".pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", report,
    )
    node_set(task_id, "deliver", "done", output_ref=json.dumps(
        {"file_id": file_row["id"]}, ensure_ascii=False), ended_at=db.now_iso())
    await _emit_node(emit, task_id, labels, "deliver", "done", detail=file_row["name"])
    _touch(task_id, "delivered", "deliver")
    return {"ok": True, "file_id": file_row["id"], "report": report, "retried": retried}


async def _retry_content(user, task, meta: dict, emit: Emit, problems: list[str]) -> dict:
    """validate 失败后的内容重写：只重跑 content，不动拆纲与材料。"""
    task_id = task["id"]
    labels = _labels(meta)
    params = params_of(task)
    resolved = models.resolve_model(None, params, user)
    effort = user["pref_effort"] or config.DEFAULT_EFFORT
    goal = task["goal"]
    schema = (meta.get("params_schema") or {}).get("page_count") or {}
    page_count = int(params.get("page_count") or schema.get("default") or 10)
    plan = node_out(task_id, "plan")
    structure = node_out(task_id, "content")
    materials, mats = materials_text(task_id)
    system = prompts.build_system("slides", skills_loader.active_prompt_skills(user["id"]), materials)
    usable = [m for m in mats if m.get("chars")]
    materials_hint = f"可用材料 {len(usable)} 份：数字与事实必须出自材料；" if usable else "没有上传材料：不要编造具体数字；"
    extra = ("文件校验发现以下问题，请修正后重新输出完整 JSON：\n- " + "\n- ".join(problems)
             + "\n上一版大纲如下，只修问题不改整体结构：\n"
             + json.dumps({"title": structure.get("title"), "slides": [
                 {"layout": s.get("layout"), "title": s.get("title"), "bullets": s.get("bullets")}
                 for s in structure.get("slides") or []]}, ensure_ascii=False))
    await _emit_node(emit, task_id, labels, "content", "running", detail="按校验意见重写")
    node_set(task_id, "content", "running", started_at=db.now_iso())
    prompt = _content_prompt(goal, page_count, plan, materials_hint, extra=extra)
    candidate = await _content_once(system, prompt, user["id"], effort, resolved.spec.id)
    errors = validate_slides(candidate, page_count)
    if errors:
        raise ValueError("；".join(errors))
    node_set(task_id, "content", "done", ended_at=db.now_iso())
    save_node_out(task_id, "content", candidate)
    await _emit_node(emit, task_id, labels, "content", "done", detail=f"{len(candidate['slides'])} 页（重写）")
    return candidate


async def revise(user, task, feedback: str, emit: Emit) -> dict:
    """按修改意见重跑 content，下游作废，再次进入待确认。额度由路由层预扣。"""
    task_id = task["id"]
    if task["status"] != "waiting_approval":
        return {"ok": False, "error": "当前不在待确认状态"}
    meta, snap = load_flow(task)
    labels = _labels(meta)
    await _emit_node(emit, task_id, labels, "content", "running", detail="按修改意见重写")
    node_set(task_id, "content", "running", started_at=db.now_iso())
    try:
        structure = node_out(task_id, "content")
        params = params_of(task)
        resolved = models.resolve_model(None, params, user)
        effort = user["pref_effort"] or config.DEFAULT_EFFORT
        schema = (meta.get("params_schema") or {}).get("page_count") or {}
        page_count = int(params.get("page_count") or schema.get("default") or 10)
        plan = node_out(task_id, "plan")
        materials, mats = materials_text(task_id)
        system = prompts.build_system("slides", skills_loader.active_prompt_skills(user["id"]), materials)
        usable = [m for m in mats if m.get("chars")]
        materials_hint = f"可用材料 {len(usable)} 份：数字与事实必须出自材料；" if usable else "没有上传材料：不要编造具体数字；"
        extra = (
            f"用户已确认过大纲方向，现在提出修改意见：{feedback}\n"
            "请按意见调整后重新输出完整 JSON（含每页 notes）。上一版供参照：\n"
            + json.dumps({"title": structure.get("title"), "slides": [
                {"layout": s.get("layout"), "title": s.get("title"), "bullets": s.get("bullets")}
                for s in structure.get("slides") or []]}, ensure_ascii=False)
        )
        candidate = await _content_once(system, _content_prompt(
            task["goal"], page_count, plan, materials_hint, extra=extra),
            user["id"], effort, resolved.spec.id)
        errors = validate_slides(candidate, page_count)
        if errors:
            raise ValueError("；".join(errors))
    except Exception as exc:
        await fail_node(emit, task_id, "content", f"修改失败：{exc}")
        _touch(task_id, "waiting_approval", "approval")
        return {"ok": False, "error": f"修改失败：{exc}"}

    save_node_out(task_id, "content", candidate)
    node_set(task_id, "content", "done", ended_at=db.now_iso())
    invalidate_downstream(task_id, "content")
    await _emit_node(emit, task_id, labels, "content", "done", detail=f"{len(candidate['slides'])} 页（已修改）")
    _touch(task_id, "waiting_approval", "approval")
    await emit("approval", _approval_payload(candidate, snap, revised=True))
    return {"ok": True, "waiting_approval": True, "slides": len(candidate["slides"])}
