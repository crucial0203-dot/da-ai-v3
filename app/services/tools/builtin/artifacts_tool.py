"""制品与文件工具。"""
from __future__ import annotations

from .... import db
from ... import artifacts
from ..executor import ToolError, register


@register("artifact_create")
async def artifact_create_tool(ctx: dict, tool_input: dict) -> dict:
    title = str(tool_input.get("title") or "未命名制品").strip()[:120]
    html = str(tool_input.get("html") or "")
    if not html.strip():
        raise ToolError("artifact_create 需要 html 参数")
    user_id = ctx.get("user_id") or ""
    if not user_id:
        raise ToolError("缺少用户上下文")
    try:
        art_id = artifacts.create_artifact(
            user_id, title, html,
            css=str(tool_input.get("css") or ""),
            js=str(tool_input.get("js") or ""),
            task_id=ctx.get("task_id"),
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return {
        "artifact_id": art_id,
        "title": title,
        "render_url": f"/api/artifacts/{art_id}/render",
        "text": f"已创建制品「{title}」，可在右侧抽屉预览。",
    }


@register("file_list")
async def file_list_tool(ctx: dict, tool_input: dict) -> dict:
    task_id = ctx.get("task_id")
    user_id = ctx.get("user_id") or ""
    if not task_id:
        raise ToolError("这个工具只能在任务里用")
    rows = db.rows(
        "SELECT id, name, size, kind, created_at FROM files "
        "WHERE task_id = ? AND user_id = ? ORDER BY created_at",
        (task_id, user_id),
    )
    files = [dict(r) for r in rows]
    if not files:
        return {"files": [], "text": "这个任务还没有上传任何文件。"}
    lines = [f"- {f['name']}（{f['size']} 字节，{f['kind']}）" for f in files]
    return {"files": files, "text": "当前任务的文件：\n" + "\n".join(lines)}
