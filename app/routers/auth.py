"""邀请码进入、会话、个人设置。

没有注册与密码登录：访客只凭邀请码换一个身份，之后靠 HttpOnly Cookie 保持会话。
管理员单独走密码（站长本人），会话 kind 为 admin，与访客完全分开。
"""
from __future__ import annotations

import hmac
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import config, db, deps, security
from ..services import models

router = APIRouter(prefix="/api", tags=["auth"])

NICKNAME_RE = re.compile(r"^[\w\u4e00-\u9fff·\-]{2,20}$")


class JoinIn(BaseModel):
    code: str = Field(min_length=4, max_length=32)
    nickname: str = Field(min_length=2, max_length=20)


class ProfileIn(BaseModel):
    nickname: str | None = None
    pref_effort: str | None = None
    pref_keep_thinking: bool | None = None
    pref_model: str | None = None


class AdminLoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)


def _cookie_secure(request: Request) -> bool:
    """按请求实际协议决定 Secure，本地 http 调试也能登录。"""
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    return request.url.scheme == "https" or proto == "https"


def _set_cookie(response: Response, token: str, request: Request, days: float) -> None:
    response.set_cookie(
        key=config.SESSION_COOKIE,
        value=token,
        max_age=int(days * 86400),
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


def _issue_session(kind: str, subject_id: str, request: Request, response: Response,
                   ttl_days: float) -> None:
    token, token_hash = security.new_session_token()
    now = db.datetime.now(db.TZ)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO sessions(token_hash, kind, subject_id, created_at, expires_at, ua, ip) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                token_hash,
                kind,
                subject_id,
                now.isoformat(timespec="seconds"),
                (now + timedelta(days=ttl_days)).isoformat(timespec="seconds"),
                (request.headers.get("user-agent") or "")[:200],
                request.client.host if request.client else "",
            ),
        )
        if kind == "visitor":
            conn.execute(
                "UPDATE visitors SET last_seen = ? WHERE id = ?", (db.now_iso(), subject_id)
            )
    _set_cookie(response, token, request, ttl_days)


@router.post("/join")
def join(payload: JoinIn, request: Request, response: Response):
    deps.limit_join(request)
    nickname = payload.nickname.strip()
    if not NICKNAME_RE.match(nickname):
        raise HTTPException(status_code=400, detail="昵称 2–20 字，只能用中英文、数字、下划线或短横线")
    code = payload.code.strip().upper()

    # 单语句原子兑换：并发下只有一个请求能把 used 推到上限，先查后改会超发
    changed = db.atomic(
        lambda conn: conn.execute(
            "UPDATE invites SET used = used + 1 WHERE code = ? AND used < max_uses "
            "AND disabled = 0 AND expires_at > ?",
            (code, db.now_iso()),
        ).rowcount
    )
    if not changed:
        invite = db.one("SELECT * FROM invites WHERE code = ?", (code,))
        if not invite:
            raise HTTPException(status_code=400, detail="邀请码不存在")
        if invite["disabled"]:
            raise HTTPException(status_code=400, detail="这个邀请码已停用")
        if invite["expires_at"] <= db.now_iso():
            raise HTTPException(status_code=400, detail="这个邀请码已过期")
        raise HTTPException(status_code=400, detail="这个邀请码的使用次数已用完")

    visitor_id = db.new_id("v_")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO visitors(id, nickname, invite_code, role, pref_effort, "
            "pref_keep_thinking, created_at) VALUES(?,?,?,'visitor',?,1,?)",
            (visitor_id, nickname, code, config.DEFAULT_EFFORT, db.now_iso()),
        )
    db.add_audit(visitor_id, "visitor.join", code, {"nickname": nickname})
    _issue_session("visitor", visitor_id, request, response, config.SESSION_TTL_DAYS)
    return {"ok": True, "nickname": nickname}


@router.post("/admin/login")
def admin_login(payload: AdminLoginIn, request: Request, response: Response):
    deps.limit_login(request)
    if not config.ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="管理员密码未配置")
    # 必须先编码：compare_digest 不接受非 ASCII 的 str，中文口令会抛 TypeError 变 500。
    # 常数时间比较的语义不变，比较的是同一编码下的字节序列。
    password = (payload.password or "").encode("utf-8")
    expected = (config.ADMIN_PASSWORD or "").encode("utf-8")
    if not (password and expected) or not hmac.compare_digest(password, expected):
        raise HTTPException(status_code=401, detail="密码不正确")
    _issue_session("admin", "admin", request, response, config.ADMIN_SESSION_HOURS / 24)
    db.add_audit("admin", "admin.login", "", {})
    return {"ok": True}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(config.SESSION_COOKIE)
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (security.sha256_hex(token),))
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    actor = deps.current_actor(request)
    payload = {
        "id": actor["id"],
        "nickname": actor["nickname"],
        "role": actor["role"],
        "kind": actor["kind"],
        "pref_effort": actor.get("pref_effort") or config.DEFAULT_EFFORT,
        "pref_keep_thinking": bool(actor.get("pref_keep_thinking", 1)),
        # 迁移没跑成时这一列不存在，.get 天然返回 None，前端回落到服务端默认
        "pref_model": actor.get("pref_model") or "",
        "quota": deps.quota_detail(actor) if actor["kind"] == "visitor" else None,
    }
    return payload


@router.patch("/me")
def update_me(payload: ProfileIn, request: Request):
    actor = deps.current_visitor(request)
    fields: list[str] = []
    params: list = []
    if payload.nickname:
        nickname = payload.nickname.strip()
        if not NICKNAME_RE.match(nickname):
            raise HTTPException(status_code=400, detail="昵称 2–20 字，只能用中英文、数字、下划线或短横线")
        fields.append("nickname = ?")
        params.append(nickname)
    if models.normalize_effort(payload.pref_effort):
        fields.append("pref_effort = ?")
        params.append(payload.pref_effort)
    if payload.pref_keep_thinking is not None:
        fields.append("pref_keep_thinking = ?")
        params.append(1 if payload.pref_keep_thinking else 0)
    if payload.pref_model is not None:
        # 迁移没跑成时这一列不存在：不报 500，只是这次设置不生效
        if not db.has_column("visitors", "pref_model"):
            raise HTTPException(status_code=503, detail="服务端数据库还没准备好，稍后再试")
        # 空串表示"清掉偏好，跟服务端默认"；非空但不在册的一律拒，避免库里存脏值
        if payload.pref_model == "":
            fields.append("pref_model = ?")
            params.append(None)
        elif models.get(payload.pref_model):
            fields.append("pref_model = ?")
            params.append(payload.pref_model)
        else:
            raise HTTPException(status_code=400, detail="这个模型不在可用清单里")
    if not fields:
        return {"ok": True}
    params.append(actor["id"])
    db.execute(f"UPDATE visitors SET {', '.join(fields)} WHERE id = ?", params)
    return {"ok": True}


@router.get("/auth/sessions")
def list_sessions(request: Request):
    actor = deps.current_actor(request)
    rows = db.rows(
        "SELECT created_at, expires_at, ua, ip FROM sessions WHERE subject_id = ? "
        "ORDER BY created_at DESC",
        (actor["id"],),
    )
    return [dict(r) for r in rows]
