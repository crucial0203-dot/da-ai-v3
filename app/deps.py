"""请求级依赖：身份、管理员守卫、限流、按次配额。

身份只有两种，都放 `sessions` 表：
- `kind='visitor'`：凭邀请码进入的访客，`subject_id` 指向 `visitors.id`
- `kind='admin'`：站长，`subject_id` 固定为 `admin`，不进 `visitors` 表

配额按「次」计，额度由邀请码决定（V3.1 起）：
  管理员单人覆盖 quota_override > 邀请码 daily_quota > 服务端兜底 config.DAILY_CALL_QUOTA。
判定与扣减都走 `ai_calls`：
先 `BEGIN IMMEDIATE` 拿写锁 → 统计已用 → 未满则插一条 reserved → 提交。
并发请求因此串行，第二个必然看到满额；进程被 kill 时 reserved 过期自动失效。
"""
from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Deque

from fastapi import HTTPException, Request

from . import config, db, security


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


# ---- 身份 ----

def actor_from_request(request: Request) -> dict | None:
    """会话 → 身份。过期会话直接当作未登录，不做后台清理。"""
    token = request.cookies.get(config.SESSION_COOKIE)
    if not token:
        return None
    token_hash = security.sha256_hex(token)
    session = db.one("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,))
    if not session:
        return None
    if session["expires_at"] <= db.now_iso():
        return None

    if session["kind"] == "admin":
        return {
            "kind": "admin",
            "id": "admin",
            "nickname": config.ADMIN_NICKNAME,
            "role": "admin",
            "pref_effort": config.DEFAULT_EFFORT,
            "pref_keep_thinking": 1,
            "quota_override": None,
        }

    visitor = db.one("SELECT * FROM visitors WHERE id = ?", (session["subject_id"],))
    if not visitor or visitor["disabled"]:
        return None
    row = dict(visitor)
    row["kind"] = "visitor"
    row["role"] = "visitor"
    return row


def current_actor(request: Request) -> dict:
    actor = actor_from_request(request)
    if not actor:
        raise HTTPException(status_code=401, detail="请先用邀请码进入")
    return actor


def current_visitor(request: Request) -> dict:
    """需要真实访客身份的接口（任务、文件、对话）。管理员不能顶替访客。"""
    actor = current_actor(request)
    if actor["kind"] != "visitor":
        raise HTTPException(status_code=403, detail="这个操作需要访客身份")
    return actor


# 旧名字保留：tasks / files / skills 里都用 current_user，语义等同访客
current_user = current_visitor


def require_admin(request: Request) -> dict:
    actor = current_actor(request)
    if actor["kind"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return actor


def visitor_is_active(visitor: dict) -> bool:
    return not visitor.get("disabled")


# ---- 内存限流：单机小并发够用，重启即清空 ----
_buckets: dict[str, Deque[float]] = defaultdict(deque)


def rate_limit(key: str, limit: int, window_sec: float) -> None:
    now = time.time()
    bucket = _buckets[key]
    while bucket and now - bucket[0] > window_sec:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
    bucket.append(now)


def limit_login(request: Request) -> None:
    rate_limit(f"login:{_client_ip(request)}", 8, 60)


def limit_join(request: Request) -> None:
    rate_limit(f"join:{_client_ip(request)}", 12, 300)


def limit_chat(visitor_id: str) -> None:
    rate_limit(f"chat:{visitor_id}", 6, 60)


# ---- 配额 ----

def invite_daily_quota(invite_code: str | None) -> int | None:
    """邀请码自带的每日额度；码不存在/迁移未跑/码没设值 → None。"""
    if not invite_code or not db.has_column("invites", "daily_quota"):
        return None
    row = db.one("SELECT daily_quota FROM invites WHERE code = ?", (invite_code,))
    if row and row["daily_quota"]:
        return int(row["daily_quota"])
    return None


def resolve_daily_quota(invite_code: str | None, quota_override: int | None) -> int:
    """额度解析唯一入口：单人覆盖 > 码额度 > 兜底。"""
    if quota_override:
        return int(quota_override)
    from_code = invite_daily_quota(invite_code)
    if from_code:
        return from_code
    return config.DAILY_CALL_QUOTA


def quota_for(actor: dict) -> int:
    return resolve_daily_quota(actor.get("invite_code"), actor.get("quota_override"))


def usage_today(visitor_id: str) -> dict:
    row = db.one(
        "SELECT * FROM usage_daily WHERE visitor_id = ? AND day = ?", (visitor_id, db.today())
    )
    if not row:
        return {"calls": 0, "in_tokens": 0, "out_tokens": 0, "cache_read": 0}
    return {
        "calls": row["calls"],
        "in_tokens": row["in_tokens"],
        "out_tokens": row["out_tokens"],
        "cache_read": row["cache_read"],
    }


def used_calls_today(visitor_id: str) -> int:
    """已用次数 = 已完成的 + 正在跑且未过期的预扣。"""
    row = db.one(
        "SELECT COUNT(*) AS n FROM ai_calls WHERE visitor_id = ? AND day = ? "
        "AND (status = 'done' OR (status = 'reserved' AND expires_at > ?))",
        (visitor_id, db.today(), db.now_iso()),
    )
    return int(row["n"] or 0)


def reserve_call(visitor_id: str, kind: str) -> tuple[str | None, str]:
    """原子预扣一次调用。

    返回 (request_id, reason)：
      ("abc...", "")      预扣成功
      (None, "running")   这个访客上一次调用还没结束——同时只放一个
      (None, "quota")     今天的额度用完了

    必须在调用模型之前、开流之前调用：SSE 一旦开始写就无法再改状态码。
    """
    request_id = uuid.uuid4().hex
    visitor = db.one("SELECT quota_override, invite_code FROM visitors WHERE id = ?", (visitor_id,))
    limit = config.DAILY_CALL_QUOTA
    if visitor:
        limit = resolve_daily_quota(visitor["invite_code"], visitor["quota_override"])

    day = db.today()
    now = datetime.now(db.TZ)

    def _reserve(conn) -> tuple[str | None, str]:
        alive = (
            "SELECT COUNT(*) AS n FROM ai_calls WHERE visitor_id = ? AND day = ? "
            "AND (status = 'done' OR (status = 'reserved' AND expires_at > ?))"
        )
        running = conn.execute(
            "SELECT COUNT(*) AS n FROM ai_calls WHERE visitor_id = ? "
            "AND status = 'reserved' AND expires_at > ?",
            (visitor_id, db.now_iso()),
        ).fetchone()
        if int(running["n"] or 0) >= 1:
            return None, "running"
        row = conn.execute(alive, (visitor_id, day, db.now_iso())).fetchone()
        if int(row["n"] or 0) >= limit:
            return None, "quota"
        conn.execute(
            "INSERT INTO ai_calls(request_id, visitor_id, day, kind, status, created_at, expires_at) "
            "VALUES(?,?,?,?, 'reserved', ?, ?)",
            (
                request_id,
                visitor_id,
                day,
                kind,
                now.isoformat(timespec="seconds"),
                (now + timedelta(seconds=config.RESERVE_TTL_SEC)).isoformat(timespec="seconds"),
            ),
        )
        return request_id, ""

    return db.atomic(_reserve)


def settle_call(
    request_id: str,
    ok: bool,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cache_read: int = 0,
) -> None:
    """结算：成功计一次额度，失败不计（用户不该为一次报错买单）。"""
    row = db.one("SELECT visitor_id FROM ai_calls WHERE request_id = ?", (request_id,))
    if not row:
        return
    visitor_id = row["visitor_id"]
    day = db.today()

    def _settle(conn) -> None:
        conn.execute(
            "UPDATE ai_calls SET status = ?, in_tokens = ?, out_tokens = ? WHERE request_id = ?",
            ("done" if ok else "failed", tokens_in, tokens_out, request_id),
        )
        if not ok:
            return
        conn.execute(
            "INSERT INTO usage_daily(visitor_id, day, in_tokens, out_tokens, cache_read, calls) "
            "VALUES(?,?,?,?,?,1) ON CONFLICT(visitor_id, day) DO UPDATE SET "
            "in_tokens = in_tokens + excluded.in_tokens, "
            "out_tokens = out_tokens + excluded.out_tokens, "
            "cache_read = cache_read + excluded.cache_read, "
            "calls = calls + 1",
            (visitor_id, day, tokens_in, tokens_out, cache_read),
        )

    db.atomic(_settle)


def record_usage(visitor_id: str, tokens_in: int, tokens_out: int, cache_read: int,
                 count_call: bool = False) -> None:
    """只记 token；count_call=True 时才把次数 +1。

    生成分章会多次调用模型，但对外只算一次额度，所以次数由 settle_call 负责。
    """
    day = db.today()
    db.atomic(
        lambda conn: conn.execute(
            "INSERT INTO usage_daily(visitor_id, day, in_tokens, out_tokens, cache_read, calls) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(visitor_id, day) DO UPDATE SET "
            "in_tokens = in_tokens + excluded.in_tokens, "
            "out_tokens = out_tokens + excluded.out_tokens, "
            "cache_read = cache_read + excluded.cache_read, "
            "calls = calls + excluded.calls",
            (visitor_id, day, tokens_in, tokens_out, cache_read, 1 if count_call else 0),
        )
    )


def quota_detail(actor: dict) -> dict:
    """给前端展示的额度信息。"""
    used = used_calls_today(actor["id"])
    limit = quota_for(actor)
    usage = usage_today(actor["id"])
    return {
        "used": used,
        "quota": limit,
        "left": max(0, limit - used),
        "calls": usage["calls"],
        "in_tokens": usage["in_tokens"],
        "out_tokens": usage["out_tokens"],
    }
