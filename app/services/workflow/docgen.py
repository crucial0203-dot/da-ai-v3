"""文档生成管线（docx / slides）：按 doc_type 走两条链，全程发 node 事件。

V4.0 起从 services/pipeline.py 迁入 workflow 包：执行逻辑归工作流执行域，
任务节点机的共享底座在 task_nodes.py，材料全文在 services/materials.py。
"""
from __future__ import annotations

import json
from pathlib import Path

from ... import config, db
from .. import docx_builder, llm, models, pptx_builder, prompts
from ..materials import materials_text
from .task_nodes import (
    Emit,
    complete_json,
    emit_node,
    fail_node,
    node_out,
    node_set,
    params_of,
    record_usage,
    register_deliverable,
    save_node_out,
    system_for,
    touch_task,
)


async def generate_task(user, task, payload: dict, emit: Emit) -> dict:
    """按 doc_type 走 docx / pptx 两条管线，全程发 node 事件。"""
    task_id = task["id"]
    resolved = models.resolve_model(payload.get("model"), params_of(task), user)
    model_id = resolved.spec.id
    effort = (
        models.normalize_effort(payload.get("effort"))
        or user["pref_effort"]
        or config.DEFAULT_EFFORT
    )
    doc_type = payload.get("doc_type") or ("pptx" if task["mode"] == "slides" else "docx")
    goal = payload.get("goal") or task["goal"]
    word_count = int(payload.get("word_count") or 1200)
    page_count = int(payload.get("page_count") or 6)

    from .task_nodes import ensure_nodes

    ensure_nodes(task_id, task["mode"])
    # 模板文件（template_id）只作版式样本，不参与材料抽取与"有无文字"判定
    skip_ids = {payload["template_id"]} if payload.get("template_id") else set()
    materials, mats = materials_text(task_id, exclude_ids=skip_ids)
    system = system_for(user["id"], task["mode"], materials)

    # intake
    await emit_node(emit, task_id, "intake", "running")
    node_set(task_id, "intake", "running", started_at=db.now_iso())
    intake = {
        "doc_type": doc_type,
        "word_count": word_count,
        "page_count": page_count,
        "materials": mats,
        "effort": effort,
        "model": model_id,
        "model_source": resolved.source,
    }
    save_node_out(task_id, "intake", intake)
    await emit_node(emit, task_id, "intake", "done",
                    detail=f"{'PPT' if doc_type == 'pptx' else '文档'} · {len(mats)} 份材料 · {model_id}")

    # source_digest：材料已由上传时抽取，这里只记录覆盖情况
    await emit_node(emit, task_id, "source_digest", "running")
    if mats:
        usable = [m for m in mats if m.get("chars")]
        images = [m for m in mats if m.get("image")]
        if not usable and not images:
            await fail_node(emit, task_id, "source_digest", "材料没有可用文字，无法作为依据")
            return {"ok": False, "error": "材料没有可用文字（可能是扫描件），请换一份或先做 OCR"}
        node_set(task_id, "source_digest", "running", started_at=db.now_iso())
        total_chars = sum(m.get("chars") or 0 for m in mats)
        save_node_out(task_id, "source_digest",
                      {"materials": mats, "chars": total_chars, "images": len(images)})
        if images and not usable:
            # 只传了图：生成链路不带图（每章都塞图成本乘章节数，且交付物里没有图的落点），
            # 所以这里如实说明，而不是假装"图被用上了"。
            detail = f"{len(images)} 张图片未参与生成（图片只在对话里可读）"
        elif images:
            detail = f"{total_chars} 字 · {len(images)} 张图片未参与生成"
        else:
            detail = f"{total_chars} 字"
        await emit_node(emit, task_id, "source_digest", "done", detail=detail)
    else:
        save_node_out(task_id, "source_digest", {"materials": [], "skipped": True})
        await emit_node(emit, task_id, "source_digest", "done", detail="无材料，按常识写作")

    if doc_type == "pptx":
        return await _generate_pptx(task_id, task, payload, system, goal, page_count, effort,
                                    model_id, emit)

    # ---- DOCX 管线 ----
    await emit_node(emit, task_id, "outline", "running")
    node_set(task_id, "outline", "running", started_at=db.now_iso())
    try:
        structure = await complete_json(
            system, prompts.outline_prompt(goal, word_count), user["id"], effort, 2000,
            model=model_id,
        )
        sections = structure.get("sections") or []
        if not sections:
            raise ValueError("模型没有给出章节结构")
    except Exception as exc:
        await fail_node(emit, task_id, "outline", f"结构生成失败：{exc}")
        return {"ok": False, "error": f"结构生成失败：{exc}"}

    save_node_out(task_id, "outline", structure)
    await emit_node(emit, task_id, "outline", "done",
                    detail=f"{len(sections)} 节 · {structure.get('title', '')}")

    # 章节节点
    with db.tx() as conn:
        existing = {r["node_key"] for r in conn.execute(
            "SELECT node_key FROM task_nodes WHERE task_id = ?", (task_id,))}
        for index in range(1, len(sections) + 1):
            node_key = f"draft.c{index}"
            if node_key not in existing:
                conn.execute(
                    "INSERT INTO task_nodes(task_id, node_key, seq, deps, status) VALUES(?,?,?,?,?)",
                    (task_id, node_key, 100 + index, json.dumps(["outline"]), "todo"),
                )

    thesis = structure.get("thesis") or ""
    paragraphs: dict[int, list[str]] = {}
    failed: list[str] = []
    total = len(sections)
    for index, section in enumerate(sections, start=1):
        node_key = f"draft.c{index}"
        heading = section.get("heading") or f"第 {index} 节"
        target = int(section.get("target_words") or max(300, word_count // max(total, 1)))
        await emit_node(emit, task_id, node_key, "running", detail=heading)
        node_set(task_id, node_key, "running", started_at=db.now_iso())
        prompt = prompts.chapter_prompt(heading, section.get("points") or [], target, thesis, index, total)
        chapter_system = system + "\n\n" + prompts.CHAPTER_SYSTEM_SUFFIX
        try:
            reply = await llm.complete(
                chapter_system,
                [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
                effort=effort, max_tokens=config.MAX_TOKENS_CHAPTER, model=model_id,
            )
            record_usage(user["id"], reply)
            text = (reply["text"] or "").strip()
            if len(text) < 80:
                raise ValueError("章节内容过短")
            paras = [p.strip() for p in text.split("\n") if p.strip()]
            paragraphs[index] = paras
            words = docx_builder.count_words(text)
            save_node_out(task_id, node_key, {"heading": heading, "paras": paras, "words": words})
            node_set(task_id, node_key, "done",
                     acceptance=json.dumps({"words": words, "target": target, "ok": words >= target * 0.6},
                                           ensure_ascii=False))
            await emit_node(emit, task_id, node_key, "done", detail=f"{words} 字")
        except Exception as exc:
            failed.append(node_key)
            await fail_node(emit, task_id, node_key, str(exc))

    if not paragraphs:
        return {"ok": False, "error": "所有章节都生成失败，请稍后重试"}

    await emit_node(emit, task_id, "draft", "running")
    node_set(task_id, "draft", "running", started_at=db.now_iso())
    assembled = {
        "title": structure.get("title") or task["title"],
        "thesis": thesis,
        "sections": [
            {"heading": sections[i - 1].get("heading") or f"第 {i} 节", "paras": paragraphs[i]}
            for i in sorted(paragraphs)
        ],
    }
    save_node_out(task_id, "draft", {"sections": len(paragraphs), "failed": failed})
    await emit_node(emit, task_id, "draft", "done", detail=f"{len(paragraphs)}/{total} 节")

    # render
    await emit_node(emit, task_id, "render", "running")
    node_set(task_id, "render", "running", started_at=db.now_iso())
    fmt = docx_builder.merge_format(payload.get("format") or {})
    if payload.get("format_sample_id"):
        sample = db.one("SELECT * FROM files WHERE id = ? AND user_id = ?",
                        (payload["format_sample_id"], user["id"]))
        if sample:
            fmt = docx_builder.merge_format(docx_builder.read_sample_format(Path(sample["path"])))
    out_path = config.FILES_DIR / user["id"] / f"{db.new_id('dl_')}.docx"
    try:
        docx_builder.build_docx(assembled, out_path, fmt)
    except Exception as exc:
        await fail_node(emit, task_id, "render", f"文件生成失败：{exc}")
        return {"ok": False, "error": f"文件生成失败：{exc}"}

    report = docx_builder.verify_docx(out_path, fmt, word_count, len(assembled["sections"]))
    file_row = register_deliverable(user["id"], task_id, out_path, assembled["title"] + ".docx",
                                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                    report)
    save_node_out(task_id, "render", {"file_id": file_row["id"], "path": str(out_path)})
    await emit_node(emit, task_id, "render", "done", detail=f"{report['stats']['words']} 字")

    # qa
    await emit_node(emit, task_id, "qa", "running")
    node_set(task_id, "qa", "running", started_at=db.now_iso())
    node_set(task_id, "qa", "done" if report["ok"] else "failed",
             acceptance=json.dumps(report, ensure_ascii=False),
             error=None if report["ok"] else f"{report['total'] - report['passed']} 项待确认",
             ended_at=db.now_iso())
    await emit_node(emit, task_id, "qa", "done" if report["ok"] else "failed",
                    detail=f"通过 {report['passed']}/{report['total']}")
    touch_task(task_id, "delivered")
    return {"ok": True, "file_id": file_row["id"], "report": report, "failed_chapters": failed}


async def _generate_pptx(task_id, task, payload, system, goal, page_count, effort, model_id, emit) -> dict:
    await emit_node(emit, task_id, "outline", "running")
    node_set(task_id, "outline", "running", started_at=db.now_iso())
    try:
        structure = await complete_json(
            system, prompts.slides_prompt(goal, page_count), task["user_id"], effort, 4000,
            model=model_id,
        )
        slides = structure.get("slides") or []
        if not slides:
            raise ValueError("模型没有给出页面结构")
    except Exception as exc:
        await fail_node(emit, task_id, "outline", f"页面结构生成失败：{exc}")
        return {"ok": False, "error": f"页面结构生成失败：{exc}"}
    save_node_out(task_id, "outline", structure)
    await emit_node(emit, task_id, "outline", "done", detail=f"{len(slides)} 页")

    await emit_node(emit, task_id, "draft", "running")
    node_set(task_id, "draft", "running", started_at=db.now_iso())
    save_node_out(task_id, "draft", {"slides": len(slides)})
    node_set(task_id, "draft", "done", ended_at=db.now_iso())
    await emit_node(emit, task_id, "draft", "done", detail=f"{len(slides)} 页文案")

    await emit_node(emit, task_id, "render", "running")
    node_set(task_id, "render", "running", started_at=db.now_iso())
    template_path = None
    if payload.get("template_id"):
        row = db.one("SELECT * FROM files WHERE id = ? AND user_id = ?", (payload["template_id"], task["user_id"]))
        if row:
            template_path = Path(row["path"])
    out_path = config.FILES_DIR / task["user_id"] / f"{db.new_id('dl_')}.pptx"
    try:
        info = pptx_builder.build_pptx(structure, out_path, template_path)
    except Exception as exc:
        await fail_node(emit, task_id, "render", f"PPT 生成失败：{exc}")
        return {"ok": False, "error": f"PPT 生成失败：{exc}"}

    report = pptx_builder.verify_pptx(out_path, len(slides))
    report["mode"] = info.get("mode")
    file_row = register_deliverable(
        task["user_id"], task_id, out_path, (structure.get("title") or task["title"]) + ".pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", report,
    )
    save_node_out(task_id, "render", {"file_id": file_row["id"], "mode": info.get("mode")})
    await emit_node(emit, task_id, "render", "done", detail=f"{len(slides)} 页")

    await emit_node(emit, task_id, "qa", "running")
    node_set(task_id, "qa", "done" if report["ok"] else "failed",
             acceptance=json.dumps(report, ensure_ascii=False),
             started_at=db.now_iso(), ended_at=db.now_iso())
    await emit_node(emit, task_id, "qa", "done" if report["ok"] else "failed",
                    detail=f"通过 {report['passed']}/{report['total']}")
    touch_task(task_id, "delivered")
    return {"ok": True, "file_id": file_row["id"], "report": report}


async def rerun_node(user, task, node_key: str, emit: Emit) -> dict:
    """重跑单节点：章节节点重写该章并重新渲染；其它节点提示走整链重跑。"""
    task_id = task["id"]
    if not node_key.startswith("draft.c"):
        return {"ok": False, "error": "这个节点不能单独重跑，请重新生成整篇"}

    index = int(node_key.split(".c", 1)[1])
    outline = node_out(task_id, "outline")
    sections = outline.get("sections") or []
    if index < 1 or index > len(sections):
        return {"ok": False, "error": "章节不存在，请重新生成结构"}

    materials, _ = materials_text(task_id)
    system = system_for(user["id"], task["mode"], materials)
    # 单章重跑沿用任务快照里的模型：正篇是哪个模型写的，重跑的这章就该是那个，
    # 否则同一份文档里会出现两种笔调。
    model_id = models.resolve_model(None, params_of(task), user).spec.id
    effort = user["pref_effort"] or config.DEFAULT_EFFORT
    section = sections[index - 1]
    target = int(section.get("target_words") or 600)
    prompt = prompts.chapter_prompt(
        section.get("heading") or f"第 {index} 节", section.get("points") or [],
        target, outline.get("thesis") or "", index, len(sections),
    )
    await emit_node(emit, task_id, node_key, "running", detail="重写中")
    node_set(task_id, node_key, "running", started_at=db.now_iso())
    try:
        reply = await llm.complete(
            system + "\n\n" + prompts.CHAPTER_SYSTEM_SUFFIX,
            [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            effort=effort, max_tokens=config.MAX_TOKENS_CHAPTER, model=model_id,
        )
        record_usage(user["id"], reply)
        text = (reply["text"] or "").strip()
        if len(text) < 80:
            raise ValueError("内容过短")
        paras = [p.strip() for p in text.split("\n") if p.strip()]
        words = docx_builder.count_words(text)
        save_node_out(task_id, node_key, {"heading": section.get("heading"), "paras": paras, "words": words})
        node_set(task_id, node_key, "done",
                 acceptance=json.dumps({"words": words, "target": target, "ok": words >= target * 0.6},
                                       ensure_ascii=False))
        await emit_node(emit, task_id, node_key, "done", detail=f"{words} 字")
        return {"ok": True, "words": words}
    except Exception as exc:
        await fail_node(emit, task_id, node_key, str(exc))
        return {"ok": False, "error": str(exc)}


def assemble_from_nodes(task_id: str) -> dict | None:
    """把已通过的章节拼回全文，用于单章重写后重新渲染。"""
    outline = node_out(task_id, "outline")
    sections = outline.get("sections") or []
    if not sections:
        return None
    built = []
    for index, section in enumerate(sections, start=1):
        data = node_out(task_id, f"draft.c{index}")
        if not data.get("paras"):
            return None
        built.append({"heading": data.get("heading") or section.get("heading"), "paras": data["paras"]})
    return {
        "title": outline.get("title") or "未命名文稿",
        "thesis": outline.get("thesis") or "",
        "sections": built,
    }
