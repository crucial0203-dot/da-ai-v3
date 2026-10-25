"""V4.0 N8 制品路由。

`/render` 是**唯一**给 iframe 用的入口，它下发独立 CSP：
`default-src 'none'` + `script-src 'unsafe-inline'` + `connect-src 'none'`。
配合前端 iframe sandbox（不带 allow-same-origin），渲染页无法访问 parent。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .. import config, deps
from ..services import artifacts

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])


class ArtifactIn(BaseModel):
    title: str = Field(default="未命名制品", max_length=120)
    html: str = Field(default="", max_length=200000)
    css: str = Field(default="", max_length=100000)
    js: str = Field(default="", max_length=100000)
    task_id: str | None = None
    message_id: int | None = None


@router.get("")
def list_artifacts(request: Request, task_id: str | None = None):
    user = deps.current_user(request)
    return {"artifacts": artifacts.list_artifacts(user["id"], task_id)}


@router.post("")
def create_artifact(payload: ArtifactIn, request: Request):
    user = deps.current_user(request)
    try:
        art_id = artifacts.create_artifact(
            user["id"], payload.title, payload.html, payload.css, payload.js,
            task_id=payload.task_id, message_id=payload.message_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "id": art_id}


@router.get("/{artifact_id}")
def get_artifact(artifact_id: str, request: Request):
    user = deps.current_user(request)
    row = artifacts.get_artifact(artifact_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="制品不存在")
    return {
        "id": row["id"], "title": row["title"], "version": row["version"],
        "task_id": row["task_id"], "created_at": row["created_at"],
        "html": row["html"],
    }


@router.get("/{artifact_id}/render")
def render_artifact(artifact_id: str, request: Request):
    """渲染制品。下发独立 CSP，不继承主页面策略。"""
    user = deps.current_user(request)
    row = artifacts.get_artifact(artifact_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="制品不存在")
    html = row["html"]
    # 在 <head> 里补一条 meta CSP：即使响应头被中间层剥掉，页面自身仍受限
    meta_csp = (
        '<meta http-equiv="Content-Security-Policy" content="'
        + artifacts.RENDER_CSP.replace("'", "&apos;")
        + '">'
    )
    if "<head>" in html.lower():
        html = html.replace("<head>", "<head>\n" + meta_csp, 1)
    else:
        html = meta_csp + html
    return HTMLResponse(
        html,
        headers={
            "Content-Security-Policy": artifacts.RENDER_CSP,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300",
        },
    )


@router.post("/{artifact_id}/versions")
def create_version(artifact_id: str, payload: ArtifactIn, request: Request):
    user = deps.current_user(request)
    document = artifacts.build_document(payload.html, payload.css, payload.js)
    try:
        new_id = artifacts.create_version(artifact_id, user["id"], document)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "id": new_id}


@router.post("/{artifact_id}/restore")
def restore_version(artifact_id: str, request: Request):
    user = deps.current_user(request)
    try:
        new_id = artifacts.restore_version(artifact_id, user["id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "id": new_id}
