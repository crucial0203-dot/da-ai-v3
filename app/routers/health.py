"""健康检查：只暴露就绪信息，不含任何凭据。"""
from __future__ import annotations

import shutil
import time

from fastapi import APIRouter

from .. import config, db

router = APIRouter(tags=["health"])
BOOT_TS = time.time()


@router.get("/healthz")
def healthz():
    payload = {
        "ok": True,
        "app": config.APP_NAME,
        "model": config.MODEL,
        "default_model": None,          # 下面从注册表取，避免 config 反向依赖 services
        "migrations": db.MIGRATIONS,
        "uptime_s": round(time.time() - BOOT_TS, 1),
        "data_dir": str(config.DATA_DIR),
        "data_dir_ready": config.DATA_DIR.exists(),
        "key_configured": bool(config.MINIMAX_API_KEY),
    }
    try:
        from ..services import models

        payload["default_model"] = models.default_model_id()
    except Exception:
        pass
    try:
        row = db.one("PRAGMA journal_mode")
        payload["journal"] = row[0] if row else "?"
        payload["db"] = "ok"
    except Exception as exc:  # noqa: BLE001
        payload["db"] = f"error: {type(exc).__name__}"
        payload["ok"] = False
    try:
        usage = shutil.disk_usage(str(config.DATA_DIR))
        payload["disk_free_gb"] = round(usage.free / 1e9, 1)
    except Exception:
        payload["disk_free_gb"] = None
    payload["status"] = "ok" if payload["ok"] and payload["key_configured"] else "degraded"
    return payload


@router.get("/__health")
def legacy_health():
    return healthz()


@router.get("/__net")
def net_diagnostics():
    """联网能力诊断：各搜索源的状态与延迟。

    三档状态：
      ok        —— 源可用且探测通过
      degraded  —— 可用但降级（无 Key 兜底源）
      off       —— 未开启或缺少 Key
    """
    from ..services import web_search

    providers = web_search.available_providers()
    payload = {
        "web_search_enabled": config.WEB_SEARCH_ENABLED,
        "providers": [],
        "fts5": db.fts5_enabled(),
        "tools": [],
    }
    for provider in providers:
        item = dict(provider)
        item["status"] = (
            "off" if not provider["available"]
            else ("degraded" if provider["degraded"] else "ok")
        )
        item["latency_ms"] = None
        payload["providers"].append(item)
    try:
        from ..services.tools import executor

        payload["tools"] = executor.enabled_tool_names()
    except Exception:
        payload["tools"] = []
    return payload


# ── 客户端错误上报 ─────────────────────────────────────
# React 渲染崩溃不会冒泡到 window.onerror，前端必须主动 POST 才看得见。
# 不鉴权：崩溃可能发生在会话建立之前，那时没有身份可用。
# 但必须限流：渲染崩溃会随每次重渲染重复触发，不限流会刷爆审计表。
_CLIENT_ERR_WINDOW = 60.0
_CLIENT_ERR_MAX = 30
_client_err_hits: list[float] = []


@router.post("/api/client-error")
def client_error(payload: dict):
    now = time.time()
    _client_err_hits[:] = [t for t in _client_err_hits if now - t < _CLIENT_ERR_WINDOW]
    if len(_client_err_hits) >= _CLIENT_ERR_MAX:
        return {"ok": False, "dropped": "rate_limited"}
    _client_err_hits.append(now)

    message = str(payload.get("message") or "")[:2000]
    stack = str(payload.get("stack") or "")[:2000]
    label = str(payload.get("label") or "")[:60]
    path = str(payload.get("path") or "")[:200]
    try:
        db.add_audit(
            None,
            "client_error",
            path or label or "unknown",
            {"label": label, "message": message, "stack": stack, "path": path},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[client-error] 写审计失败：{type(exc).__name__}: {exc}", flush=True)
    # 审计写不进去也要落 stdout，免得单进程里唯一线索消失
    print(f"[client-error] {label or '-'} @ {path or '-'} :: {message[:300]}", flush=True)
    return {"ok": True}
