"""Skills 配置中心（管理员）：skill.json + SKILL.md 的版本化 CRUD。

数据模型（V3.1）：
- skills.published_version：当前发布版本号，NULL=从未发布
- skill_versions：一行一版本；body=SKILL.md，meta_json=skill.json
  （nodes / permissions / params_schema / policy）；status: draft|published|archived
- 每个 skill 至多一条 draft（version='draft'，保存即覆盖），发布后转成正式版本号
- 发布版本不可原地修改；回滚 = 把 published_version 指回历史版本

版本号规则：首个发布版本 1.0，之后 1.1、1.2 递增（minor 步进）。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import db, deps
from ..services import skill_tools

router = APIRouter(prefix="/api/admin/skills", tags=["admin-skills"])


def _load_meta(raw: str) -> dict:
    try:
        meta = json.loads(raw or "{}")
    except json.JSONDecodeError:
        meta = {}
    return meta if isinstance(meta, dict) else {}


def _skill_or_404(skill_id: str) -> dict:
    row = db.one("SELECT * FROM skills WHERE id = ?", (skill_id,))
    if not row:
        raise HTTPException(status_code=404, detail="技能不存在")
    return row


def _version_rows(skill_id: str) -> list[dict]:
    rows = db.rows(
        "SELECT id, version, status, created_by, created_at, meta_json, body "
        "FROM skill_versions WHERE skill_id = ? ORDER BY id DESC",
        (skill_id,),
    )
    return [
        {
            "id": r["id"],
            "version": r["version"],
            "status": r["status"] or "published",
            "created_by": r["created_by"],
            "created_at": r["created_at"],
            "name": _load_meta(r["meta_json"]).get("name", ""),
            "nodes": len(_load_meta(r["meta_json"]).get("nodes") or []),
            "body_len": len(r["body"] or ""),
        }
        for r in rows
    ]


def _summary(row: dict) -> dict:
    published_meta = {}
    published_at = None
    if row["published_version"]:
        prow = db.one(
            "SELECT meta_json, created_at FROM skill_versions WHERE skill_id = ? AND version = ?",
            (row["id"], row["published_version"]),
        )
        if prow:
            published_meta = _load_meta(prow["meta_json"])
            published_at = prow["created_at"]
    draft = db.one(
        "SELECT id, created_at FROM skill_versions WHERE skill_id = ? AND version = 'draft'",
        (row["id"],),
    )
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "category": row["category"],
        "enabled": bool(row["enabled"]),
        "visibility": row["visibility"],
        "is_builtin": bool(row["is_builtin"]),
        "created_at": row["created_at"],
        "published_version": row["published_version"],
        "published_at": published_at,
        "published_description": published_meta.get("description", ""),
        "has_draft": bool(draft),
        "draft_at": draft["created_at"] if draft else None,
        "version_count": db.one(
            "SELECT COUNT(*) AS n FROM skill_versions WHERE skill_id = ?", (row["id"],)
        )["n"],
    }


@router.get("")
def list_skills(request: Request):
    deps.require_admin(request)
    rows = db.rows("SELECT * FROM skills ORDER BY is_builtin DESC, created_at DESC")
    return {"skills": [_summary(r) for r in rows]}


class SkillCreate(BaseModel):
    name: str
    category: str = "工作流"
    description: str = ""
    body: str = ""            # SKILL.md
    meta: dict | None = None  # skill.json（nodes/permissions/params_schema/policy）


@router.post("")
def create_skill(payload: SkillCreate, request: Request):
    deps.require_admin(request)
    name = payload.name.strip()
    if not (1 <= len(name) <= 40):
        raise HTTPException(status_code=400, detail="技能名 1–40 字")
    if not payload.body.strip():
        raise HTTPException(status_code=400, detail="SKILL.md 内容不能为空")
    meta = payload.meta or {}
    errors = skill_tools.validate_workflow_config(meta)
    if errors:
        raise HTTPException(status_code=400, detail={"message": "配置校验未通过", "code": "config_invalid", "errors": errors})

    skill_id = db.new_id("skw_")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO skills(id, owner_id, name, version, kind, category, enabled, visibility, "
            "reviewed_by, is_builtin, published_version, created_at) "
            "VALUES(?,?,?,?, 'workflow', ?,1,'shared','admin',0,NULL,?)",
            (skill_id, "admin", name, "1.0", payload.category.strip()[:20] or "工作流", db.now_iso()),
        )
        conn.execute(
            "INSERT INTO skill_versions(skill_id, version, body, meta_json, status, created_by, created_at) "
            "VALUES(?,?,?,?, 'draft', ?, ?)",
            (skill_id, "draft", payload.body, json.dumps(meta, ensure_ascii=False), "admin", db.now_iso()),
        )
    db.add_audit("admin", "skill.create", skill_id, {"name": name})
    row = db.one("SELECT * FROM skills WHERE id = ?", (skill_id,))
    return {"ok": True, "skill": _summary(row)}


@router.get("/{skill_id}")
def get_skill(skill_id: str, request: Request):
    deps.require_admin(request)
    row = _skill_or_404(skill_id)
    data = _summary(row)
    published = None
    if row["published_version"]:
        published = db.one(
            "SELECT * FROM skill_versions WHERE skill_id = ? AND version = ?",
            (skill_id, row["published_version"]),
        )
    draft = db.one(
        "SELECT * FROM skill_versions WHERE skill_id = ? AND version = 'draft'", (skill_id,)
    )
    data["published"] = (
        {"version": published["version"], "body": published["body"],
         "meta": _load_meta(published["meta_json"])}
        if published else None
    )
    data["draft"] = (
        {"version": "draft", "body": draft["body"], "meta": _load_meta(draft["meta_json"])}
        if draft else None
    )
    data["versions"] = _version_rows(skill_id)
    return data


class SkillPatch(BaseModel):
    name: str | None = None
    category: str | None = None
    enabled: bool | None = None
    visibility: str | None = None


@router.patch("/{skill_id}")
def patch_skill(skill_id: str, payload: SkillPatch, request: Request):
    deps.require_admin(request)
    _skill_or_404(skill_id)
    fields, params = [], []
    if payload.name is not None:
        name = payload.name.strip()
        if not (1 <= len(name) <= 40):
            raise HTTPException(status_code=400, detail="技能名 1–40 字")
        fields.append("name = ?")
        params.append(name)
    if payload.category is not None:
        fields.append("category = ?")
        params.append(payload.category.strip()[:20] or "工作流")
    if payload.enabled is not None:
        fields.append("enabled = ?")
        params.append(1 if payload.enabled else 0)
    if payload.visibility is not None:
        if payload.visibility not in {"private", "shared"}:
            raise HTTPException(status_code=400, detail="可见性只能是 private 或 shared")
        fields.append("visibility = ?")
        params.append(payload.visibility)
    if not fields:
        return {"ok": True}
    params.append(skill_id)
    db.execute(f"UPDATE skills SET {', '.join(fields)} WHERE id = ?", params)
    db.add_audit("admin", "skill.patch", skill_id, payload.model_dump(exclude_none=True))
    return {"ok": True}


class DraftIn(BaseModel):
    body: str
    meta: dict


@router.put("/{skill_id}/draft")
def save_draft(skill_id: str, payload: DraftIn, request: Request):
    """保存草稿：每个 skill 只有一条 draft，保存即覆盖。发布版本不受影响。"""
    deps.require_admin(request)
    _skill_or_404(skill_id)
    if not payload.body.strip():
        raise HTTPException(status_code=400, detail="SKILL.md 内容不能为空")
    errors = skill_tools.validate_workflow_config(payload.meta or {})
    if errors:
        raise HTTPException(
            status_code=400,
            detail={"message": "配置校验未通过", "code": "config_invalid", "errors": errors},
        )
    existing = db.one(
        "SELECT id FROM skill_versions WHERE skill_id = ? AND version = 'draft'", (skill_id,)
    )
    meta_json = json.dumps(payload.meta or {}, ensure_ascii=False)
    if existing:
        db.execute(
            "UPDATE skill_versions SET body = ?, meta_json = ?, created_at = ? WHERE id = ?",
            (payload.body, meta_json, db.now_iso(), existing["id"]),
        )
    else:
        db.execute(
            "INSERT INTO skill_versions(skill_id, version, body, meta_json, status, created_by, created_at) "
            "VALUES(?,?,?,?, 'draft', 'admin', ?)",
            (skill_id, "draft", payload.body, meta_json, db.now_iso()),
        )
    db.add_audit("admin", "skill.draft", skill_id, {})
    return {"ok": True}


@router.post("/{skill_id}/publish")
def publish_skill(skill_id: str, request: Request):
    """draft → published：旧发布版本转 archived，published_version 指向新版本。"""
    deps.require_admin(request)
    row = _skill_or_404(skill_id)
    draft = db.one(
        "SELECT * FROM skill_versions WHERE skill_id = ? AND version = 'draft'", (skill_id,)
    )
    if not draft:
        raise HTTPException(status_code=400, detail="没有可发布的草稿")

    meta = _load_meta(draft["meta_json"])
    errors = skill_tools.validate_workflow_config(meta)
    if errors:
        raise HTTPException(
            status_code=400,
            detail={"message": "配置校验未通过", "code": "config_invalid", "errors": errors},
        )

    # 版本号：published_version 1.x → 1.(x+1)；从未发布则 1.0
    current = row["published_version"]
    if current and "." in current:
        try:
            major, minor = current.split(".", 1)
            new_version = f"{major}.{int(minor) + 1}"
        except ValueError:
            new_version = "1.0"
    else:
        new_version = "1.0"

    with db.tx() as conn:
        conn.execute(
            "UPDATE skill_versions SET status = 'archived' "
            "WHERE skill_id = ? AND status = 'published'",
            (skill_id,),
        )
        conn.execute(
            "UPDATE skill_versions SET version = ?, status = 'published', created_at = ? WHERE id = ?",
            (new_version, db.now_iso(), draft["id"]),
        )
        conn.execute(
            "UPDATE skills SET published_version = ?, version = ? WHERE id = ?",
            (new_version, new_version, skill_id),
        )
    db.add_audit("admin", "skill.publish", skill_id, {"version": new_version})
    return {"ok": True, "version": new_version}


class RollbackIn(BaseModel):
    version: str


@router.post("/{skill_id}/rollback")
def rollback_skill(skill_id: str, payload: RollbackIn, request: Request):
    """回滚 = published_version 指回某个历史版本，不改任何版本行内容。"""
    deps.require_admin(request)
    row = _skill_or_404(skill_id)
    target = db.one(
        "SELECT * FROM skill_versions WHERE skill_id = ? AND version = ? AND version != 'draft'",
        (skill_id, payload.version),
    )
    if not target:
        raise HTTPException(status_code=404, detail="要回滚的版本不存在")
    with db.tx() as conn:
        conn.execute(
            "UPDATE skill_versions SET status = CASE WHEN version = ? THEN 'published' ELSE 'archived' END "
            "WHERE skill_id = ? AND version != 'draft'",
            (payload.version, skill_id),
        )
        conn.execute(
            "UPDATE skills SET published_version = ?, version = ? WHERE id = ?",
            (payload.version, payload.version, skill_id),
        )
    db.add_audit("admin", "skill.rollback", skill_id, {"from": row["published_version"], "to": payload.version})
    return {"ok": True, "version": payload.version}


@router.post("/{skill_id}/disable")
def disable_skill(skill_id: str, request: Request, enabled: bool = False):
    """停用后任务列表不可再选；已绑定任务照常跑完（version 快照可解析）。"""
    deps.require_admin(request)
    _skill_or_404(skill_id)
    db.execute("UPDATE skills SET enabled = ? WHERE id = ?", (1 if enabled else 0, skill_id))
    db.add_audit("admin", "skill.disable" if not enabled else "skill.enable", skill_id, {})
    return {"ok": True, "enabled": enabled}
