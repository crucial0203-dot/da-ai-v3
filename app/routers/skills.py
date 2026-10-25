"""技能中心：上传、解析、分类、启停、审核。"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from .. import config, db, deps
from ..services import skills_loader

router = APIRouter(prefix="/api/skills", tags=["skills"])

KIND_LABEL = {
    "prompt": "提示词增强",
    "workflow": "固定流程",
    "tool": "工具调用",
    "script": "含脚本（不执行）",
}


def _row_to_skill(row, body: str = "") -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "version": row["version"],
        "kind": row["kind"],
        "kind_label": KIND_LABEL.get(row["kind"], row["kind"]),
        "category": row["category"],
        "enabled": bool(row["enabled"]),
        "visibility": row["visibility"],
        "reviewed": bool(row["reviewed_by"]),
        "is_builtin": bool(row["is_builtin"]),
        "owner_id": row["owner_id"],
        "created_at": row["created_at"],
        "use_count": row["use_count"] if "use_count" in row.keys() else 0,
        "body_preview": body[:280],
        "body_len": len(body),
    }


@router.get("")
def list_skills(request: Request, kind: str | None = None):
    user = deps.current_user(request)
    if kind == "workflow":
        # 访客端固定流程列表：只暴露管理员已发布且启用的 workflow 技能
        rows = db.rows(
            "SELECT * FROM skills WHERE kind = 'workflow' AND enabled = 1 AND published_version IS NOT NULL "
            "ORDER BY is_builtin DESC, created_at DESC"
        )
        return {"skills": [_workflow_view(row) for row in rows]}

    rows = db.rows(
        "SELECT * FROM skills WHERE is_builtin = 1 OR owner_id = ? OR visibility = 'shared' "
        "ORDER BY is_builtin DESC, created_at DESC",
        (user["id"],),
    )
    result = []
    for row in rows:
        version = db.one(
            "SELECT body FROM skill_versions WHERE skill_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)
        )
        result.append(_row_to_skill(row, version["body"] if version else ""))
    return {"skills": result}


def _workflow_view(row) -> dict:
    """workflow 技能的访客视图：只读 config，不含 permissions/policy 内部字段。"""
    meta: dict = {}
    body = ""
    if row["published_version"]:
        prow = db.one(
            "SELECT body, meta_json FROM skill_versions WHERE skill_id = ? AND version = ?",
            (row["id"], row["published_version"]),
        )
        if prow:
            try:
                meta = json.loads(prow["meta_json"] or "{}")
            except json.JSONDecodeError:
                meta = {}
            body = prow["body"] or ""
    nodes = [
        {"key": n.get("key", ""), "label": n.get("label", ""), "deps": list(n.get("deps") or [])}
        for n in (meta.get("nodes") or [])
        if isinstance(n, dict)
    ]
    return {
        "id": row["id"],
        "name": row["name"],
        "version": row["published_version"],
        "kind": row["kind"],
        "kind_label": KIND_LABEL.get(row["kind"], row["kind"]),
        "category": row["category"],
        "enabled": bool(row["enabled"]),
        "is_builtin": bool(row["is_builtin"]),
        "description": meta.get("description", ""),
        "nodes": nodes,
        "params_schema": meta.get("params_schema") or {},
        "body": body,
        "created_at": row["created_at"],
    }


@router.get("/{skill_id}")
def get_skill(skill_id: str, request: Request):
    user = deps.current_user(request)
    row = db.one("SELECT * FROM skills WHERE id = ?", (skill_id,))
    if not row:
        raise HTTPException(status_code=404, detail="技能不存在")
    if row["kind"] == "workflow":
        # workflow 技能详情 = 已发布版本的只读视图
        if not row["published_version"] or not row["enabled"]:
            raise HTTPException(status_code=404, detail="技能未发布或已停用")
        return _workflow_view(row)
    version = db.one("SELECT body FROM skill_versions WHERE skill_id = ? ORDER BY id DESC LIMIT 1", (skill_id,))
    data = _row_to_skill(row, version["body"] if version else "")
    data["body"] = version["body"] if version else ""
    return data


@router.post("")
async def upload_skill(
    request: Request,
    file: UploadFile | None = File(default=None),
    name: str | None = Form(default=None),
    category: str = Form(default="通用"),
    body: str | None = Form(default=None),
):
    user = deps.current_user(request)
    filenames: list[str] = []
    text = (body or "").strip()
    original_name = name or ""

    if file is not None:
        raw = await file.read()
        if len(raw) > 2 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="技能文件不要超过 2MB")
        suffix = Path(file.filename or "").suffix.lower()
        original_name = original_name or Path(file.filename or "未命名技能").stem
        if suffix == ".zip":
            tmp_dir = config.DATA_DIR / "tmp" / uuid.uuid4().hex
            tmp_dir.mkdir(parents=True, exist_ok=True)
            zip_path = tmp_dir / "upload.zip"
            zip_path.write_bytes(raw)
            try:
                extracted = skills_loader.safe_unzip(zip_path, tmp_dir / "unpacked")
            except ValueError as exc:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise HTTPException(status_code=400, detail=str(exc))
            filenames = [p.name for p in extracted]
            skill_md = next((p for p in extracted if p.name.lower() == "skill.md"), None)
            if skill_md is None:
                skill_md = next((p for p in extracted if p.suffix.lower() in {".md", ".txt"}), None)
            if skill_md is None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise HTTPException(status_code=400, detail="压缩包里没有找到 SKILL.md 或说明文档")
            text = skill_md.read_text(encoding="utf-8", errors="replace")
            shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(status_code=400, detail="技能文件需为 UTF-8 文本")
            filenames = [file.filename or "skill.md"]

    if not text:
        raise HTTPException(status_code=400, detail="技能内容为空")

    meta, body_text = skills_loader.parse_front_matter(text)
    kind = skills_loader.classify(body_text, filenames)
    skill_name = skills_loader.derive_name(body_text, meta, original_name or "未命名技能")
    skill_id = db.new_id("sk_")
    version = meta.get("version") or "1.0.0"

    with db.tx() as conn:
        conn.execute(
            "INSERT INTO skills(id, owner_id, name, version, kind, category, enabled, visibility, "
            "reviewed_by, is_builtin, created_at) VALUES(?,?,?,?,?,?,1,'private',NULL,0,?)",
            (skill_id, user["id"], skill_name, version, kind, category, db.now_iso()),
        )
        conn.execute(
            "INSERT INTO skill_versions(skill_id, version, body, meta_json, created_at) VALUES(?,?,?,?,?)",
            (skill_id, version, body_text, json.dumps({"files": filenames, "meta": meta}, ensure_ascii=False),
             db.now_iso()),
        )
    db.add_audit(user["id"], "skill.upload", skill_id, {"name": skill_name, "kind": kind})
    row = db.one("SELECT * FROM skills WHERE id = ?", (skill_id,))
    return {"ok": True, "skill": _row_to_skill(row, body_text)}


class SkillPatch(BaseModel):
    name: str | None = None
    category: str | None = None
    enabled: bool | None = None
    visibility: str | None = None


@router.patch("/{skill_id}")
def patch_skill(skill_id: str, payload: SkillPatch, request: Request):
    user = deps.current_user(request)
    row = db.one("SELECT * FROM skills WHERE id = ?", (skill_id,))
    if not row:
        raise HTTPException(status_code=404, detail="技能不存在")
    is_admin = user["role"] == "admin"
    if row["owner_id"] != user["id"] and not is_admin:
        raise HTTPException(status_code=403, detail="只能修改自己上传的技能")
    if row["is_builtin"] and payload.name is not None:
        raise HTTPException(status_code=400, detail="内置技能不能改名")

    fields, params = [], []
    if payload.name:
        fields.append("name = ?")
        params.append(payload.name.strip()[:40])
    if payload.category:
        fields.append("category = ?")
        params.append(payload.category.strip()[:20])
    if payload.enabled is not None:
        fields.append("enabled = ?")
        params.append(1 if payload.enabled else 0)
    if payload.visibility:
        if payload.visibility not in {"private", "shared"}:
            raise HTTPException(status_code=400, detail="可见性只能是 private 或 shared")
        if payload.visibility == "shared" and not is_admin:
            raise HTTPException(status_code=403, detail="共享技能需要管理员审核后才能设置")
        fields.append("visibility = ?")
        params.append(payload.visibility)
        if is_admin:
            fields.append("reviewed_by = ?")
            params.append(user["id"])
    if not fields:
        return {"ok": True}
    params.append(skill_id)
    db.execute(f"UPDATE skills SET {', '.join(fields)} WHERE id = ?", params)
    return {"ok": True}


@router.post("/{skill_id}/review")
def review(skill_id: str, request: Request):
    user = deps.require_admin(request)
    db.execute("UPDATE skills SET reviewed_by = ?, visibility = 'shared' WHERE id = ?", (user["id"], skill_id))
    db.add_audit(user["id"], "skill.review", skill_id, {})
    return {"ok": True}


@router.delete("/{skill_id}")
def delete_skill(skill_id: str, request: Request):
    user = deps.current_user(request)
    row = db.one("SELECT * FROM skills WHERE id = ?", (skill_id,))
    if not row:
        raise HTTPException(status_code=404, detail="技能不存在")
    if row["is_builtin"]:
        raise HTTPException(status_code=400, detail="内置技能不能删除，可以停用")
    if row["owner_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权删除这个技能")
    with db.tx() as conn:
        conn.execute("DELETE FROM skill_versions WHERE skill_id = ?", (skill_id,))
        conn.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
    return {"ok": True}
