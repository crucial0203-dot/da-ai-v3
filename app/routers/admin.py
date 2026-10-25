"""管理台：邀请码、访客、审计。只有管理员会话能进。"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import config, db, deps, security

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _invite_state(invite: dict) -> str:
    if invite["disabled"]:
        return "disabled"
    if invite["used"] >= invite["max_uses"]:
        return "used_up"
    if invite["expires_at"] <= db.now_iso():
        return "expired"
    return "ok"


@router.post("/settings")
def admin_set_settings(payload: dict, request: Request):
    """改运行时设置。

    只开放白名单里的键——否则这就是一个任意写的后门。
    """
    deps.require_admin(request)
    allowed = {"fts5_enabled"}
    changed: dict[str, str] = {}
    for key, value in (payload or {}).items():
        if key not in allowed:
            continue
        db.set_setting(key, str(value))
        changed[key] = str(value)
    if "fts5_enabled" in changed:
        db.FTS_AVAILABLE = changed["fts5_enabled"] == "1"
    return {"ok": True, "changed": changed}


def _invite_rows() -> list[dict]:
    rows = db.rows(
        "SELECT i.*, "
        "(SELECT COUNT(*) FROM visitors v WHERE v.invite_code = i.code) AS joined "
        "FROM invites i ORDER BY i.created_at DESC LIMIT 200"
    )
    items = []
    for row in rows:
        item = dict(row)
        item["state"] = _invite_state(item)
        item["left"] = max(0, item["max_uses"] - item["used"])
        items.append(item)
    return items


@router.get("/overview")
def overview(request: Request):
    deps.require_admin(request)
    visitor_count = db.one("SELECT COUNT(*) AS n FROM visitors")["n"]
    task_count = db.one("SELECT COUNT(*) AS n FROM tasks")["n"]
    files = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM files")
    today = db.one(
        "SELECT COALESCE(SUM(calls),0) AS c, COALESCE(SUM(in_tokens),0) AS i, "
        "COALESCE(SUM(out_tokens),0) AS o FROM usage_daily WHERE day = ?",
        (db.today(),),
    )
    return {
        "visitors": visitor_count,
        "tasks": task_count,
        "files": files["n"],
        "file_bytes": files["bytes"],
        "today": {"calls": today["c"], "in": today["i"], "out": today["o"]},
        "quota_default": config.DAILY_CALL_QUOTA,
        "invites": _invite_rows(),
        "data_dir": config.data_dir_hint(),
        "model": config.MODEL,
        "key_configured": bool(config.MINIMAX_API_KEY),
    }


@router.get("/visitors")
def list_visitors(request: Request):
    deps.require_admin(request)
    # 码额度子查询要防迁移降级：列缺失时退回常量 NULL，前端走兜底展示
    invite_quota = (
        "(SELECT i.daily_quota FROM invites i WHERE i.code = v.invite_code) AS invite_quota"
        if db.has_column("invites", "daily_quota")
        else "NULL AS invite_quota"
    )
    rows = db.rows(
        f"SELECT v.id, v.nickname, v.invite_code, v.created_at, v.last_seen, v.disabled, "
        f"v.quota_override, {invite_quota}, "
        "(SELECT COUNT(*) FROM tasks t WHERE t.user_id = v.id) AS tasks, "
        "(SELECT COUNT(*) FROM ai_calls c WHERE c.visitor_id = v.id AND c.day = ? "
        " AND (c.status='done' OR (c.status='reserved' AND c.expires_at > ?))) AS calls_today "
        "FROM visitors v ORDER BY v.created_at DESC LIMIT 500",
        (db.today(), db.now_iso()),
    )
    return {"visitors": [dict(r) for r in rows]}


class VisitorPatch(BaseModel):
    quota_override: int | None = None
    disabled: bool | None = None


@router.patch("/visitors/{visitor_id}")
def patch_visitor(visitor_id: str, payload: VisitorPatch, request: Request):
    deps.require_admin(request)
    target = db.one("SELECT * FROM visitors WHERE id = ?", (visitor_id,))
    if not target:
        raise HTTPException(status_code=404, detail="访客不存在")
    fields, params = [], []
    if payload.quota_override is not None:
        fields.append("quota_override = ?")
        params.append(max(0, payload.quota_override))
    if payload.disabled is not None:
        fields.append("disabled = ?")
        params.append(1 if payload.disabled else 0)
    if not fields:
        return {"ok": True}
    params.append(visitor_id)
    db.execute(f"UPDATE visitors SET {', '.join(fields)} WHERE id = ?", params)
    db.add_audit("admin", "visitor.patch", visitor_id, payload.model_dump())
    return {"ok": True}


@router.delete("/visitors/{visitor_id}")
def delete_visitor(visitor_id: str, request: Request):
    deps.require_admin(request)
    target = db.one("SELECT * FROM visitors WHERE id = ?", (visitor_id,))
    if not target:
        raise HTTPException(status_code=404, detail="访客不存在")
    files = db.rows("SELECT path FROM files WHERE user_id = ?", (visitor_id,))
    with db.tx() as conn:
        conn.execute("DELETE FROM messages WHERE task_id IN (SELECT id FROM tasks WHERE user_id = ?)", (visitor_id,))
        conn.execute("DELETE FROM task_nodes WHERE task_id IN (SELECT id FROM tasks WHERE user_id = ?)", (visitor_id,))
        conn.execute("DELETE FROM files WHERE user_id = ?", (visitor_id,))
        conn.execute("DELETE FROM tasks WHERE user_id = ?", (visitor_id,))
        conn.execute("DELETE FROM ai_calls WHERE visitor_id = ?", (visitor_id,))
        conn.execute("DELETE FROM usage_daily WHERE visitor_id = ?", (visitor_id,))
        conn.execute("DELETE FROM sessions WHERE subject_id = ?", (visitor_id,))
        conn.execute("DELETE FROM visitors WHERE id = ?", (visitor_id,))
    for item in files:
        try:
            Path(item["path"]).unlink(missing_ok=True)
        except Exception:
            pass
    shutil.rmtree(config.FILES_DIR / visitor_id, ignore_errors=True)
    db.add_audit("admin", "visitor.delete", visitor_id, {"nickname": target["nickname"]})
    return {"ok": True}


class InviteIn(BaseModel):
    count: int = 1
    max_uses: int = 5
    days: int = 7
    daily_quota: int = 50
    note: str | None = None
    code: str | None = None


@router.post("/invites")
def create_invites(payload: InviteIn, request: Request):
    deps.require_admin(request)
    count = max(1, min(payload.count, 10))
    max_uses = max(1, min(payload.max_uses, 100))
    days = max(1, min(payload.days, 90))
    daily_quota = max(1, min(payload.daily_quota, 500))
    expires = (datetime.now(db.TZ) + timedelta(days=days)).isoformat(timespec="seconds")

    codes: list[str] = []
    with db.tx() as conn:
        for index in range(count):
            custom = (payload.code or "").strip().upper()
            if custom:
                if index > 0:
                    break  # 自定义码只能建一个
                if not (4 <= len(custom) <= 16):
                    raise HTTPException(status_code=400, detail="自定义码 4–16 位")
                if conn.execute("SELECT 1 FROM invites WHERE code = ?", (custom,)).fetchone():
                    raise HTTPException(status_code=400, detail="这个码已存在")
                code = custom
            else:
                code = security.new_invite_code()
            conn.execute(
                "INSERT INTO invites(code, max_uses, used, days, daily_quota, expires_at, note, created_at) "
                "VALUES(?,?,0,?,?,?,?,?)",
                (code, max_uses, days, daily_quota, expires, payload.note or "", db.now_iso()),
            )
            codes.append(code)
    db.add_audit(
        "admin", "invite.create", "",
        {"count": len(codes), "max_uses": max_uses, "days": days, "daily_quota": daily_quota},
    )
    return {"ok": True, "codes": codes}


class InvitePatch(BaseModel):
    disabled: bool | None = None
    daily_quota: int | None = None


@router.patch("/invites/{code}")
def patch_invite(code: str, payload: InvitePatch, request: Request):
    deps.require_admin(request)
    fields, params = [], []
    if payload.disabled is not None:
        fields.append("disabled = ?")
        params.append(1 if payload.disabled else 0)
    if payload.daily_quota is not None:
        fields.append("daily_quota = ?")
        params.append(max(1, min(payload.daily_quota, 500)))
    if not fields:
        return {"ok": True}
    params.append(code.upper())
    db.execute(f"UPDATE invites SET {', '.join(fields)} WHERE code = ?", params)
    db.add_audit("admin", "invite.patch", code, payload.model_dump(exclude_none=True))
    return {"ok": True}


@router.delete("/invites/{code}")
def delete_invite(code: str, request: Request):
    deps.require_admin(request)
    db.execute("DELETE FROM invites WHERE code = ?", (code.upper(),))
    db.add_audit("admin", "invite.delete", code, {})
    return {"ok": True}


@router.get("/audit")
def audit(request: Request, limit: int = 100):
    deps.require_admin(request)
    rows = db.rows(
        "SELECT a.*, v.nickname FROM audit_log a LEFT JOIN visitors v ON v.id = a.actor_id "
        "ORDER BY a.id DESC LIMIT ?",
        (min(limit, 300),),
    )
    return {
        "items": [
            {
                "id": r["id"],
                "actor": r["nickname"] or ("管理员" if r["actor_id"] == "admin" else "系统"),
                "action": r["action"],
                "target": r["target"],
                "detail": json.loads(r["detail_json"] or "{}"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    }
