"""插件市场 API —— 目录浏览、安装/卸载/启停、撤销。

语义对齐 dsh-market：只装目录里的（拒装未知源）、启停即时生效、操作可一步撤销。
安装状态按用户隔离；市场操作需要登录（访客即可）。

变更类接口过单飞锁（移植 dsh-market withMutationLock）：并发 install 同一个包
会双写主键冲突变成 500；第二个请求直接拿 409，让它重试。
锁用 threading.Lock 而不是 asyncio.Lock：本路由是同步 def，跑在线程池里，
asyncio.Lock 跨线程不生效。非阻塞获取，抢不到就 409——不排队，避免请求堆积。
"""
from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import deps
from ..services import market as market_service

router = APIRouter(prefix="/api/market", tags=["market"])

_mutate_lock = threading.Lock()


class _Busy(Exception):
    pass


def _locked(fn):
    """把同步市场变更包进单飞锁；抢不到锁 → 409。"""

    def wrapper(*args, **kwargs):
        if not _mutate_lock.acquire(blocking=False):
            raise _Busy()
        try:
            return fn(*args, **kwargs)
        finally:
            _mutate_lock.release()

    return wrapper


class PluginRef(BaseModel):
    id: str


class ToggleBody(BaseModel):
    id: str
    enabled: bool


def _err(exc: market_service.MarketError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"message": exc.message})


def _catalog_view(user_id: str) -> dict:
    catalog = market_service.load_catalog()
    installed = market_service.installed_index(user_id)
    plugins = []
    for entry in catalog.get("plugins", []):
        item = {k: v for k, v in entry.items() if k != "payload"}
        payload = entry.get("payload") or {}
        # 目录浏览只带轻量预览（主题色板）；模块源码走详情接口
        if entry["kind"] == "theme":
            item["preview"] = {"colorScheme": payload.get("colorScheme"), "tokens": payload.get("tokens", {})}
        elif entry["kind"] == "tool-pack":
            item["tools"] = [t.get("name") for t in payload.get("tools", [])]
        elif entry["kind"] == "ui-module":
            item["module"] = payload.get("module")
        elif entry["kind"] == "dsh-ref":
            item["no_install"] = True  # DSH 社区条目：浏览型，不可装
        rec = installed.get(entry["id"])
        item["installed"] = bool(rec)
        item["enabled"] = bool(rec["enabled"]) if rec else False
        item["installed_at"] = rec["installed_at"] if rec else None
        plugins.append(item)
    return {
        "updated": catalog.get("updated"),
        "count": len(plugins),
        "categories": catalog.get("categories", {}),
        "plugins": plugins,
        "active_theme": market_service.active_theme_id(user_id),
    }


@router.get("/catalog")
def catalog(request: Request) -> dict:
    user = deps.current_user(request)
    return _catalog_view(user["id"])


@router.get("/plugin/{plugin_id}")
def plugin_detail(request: Request, plugin_id: str) -> dict:
    deps.current_user(request)
    entry = market_service.catalog_index().get(plugin_id)
    if not entry:
        raise HTTPException(status_code=404, detail={"message": "目录里没有这个插件"})
    try:
        payload = market_service.entry_payload(entry)
    except market_service.MarketError as exc:
        raise _err(exc) from exc
    return {**entry, "payload": payload}


@router.post("/install")
def install(body: PluginRef, request: Request) -> dict:
    user = deps.current_user(request)
    try:
        row = _locked(market_service.install)(user["id"], body.id)
    except _Busy:
        raise HTTPException(status_code=409, detail={"message": "市场正在处理另一个变更，请稍后重试"}) from None
    except market_service.MarketError as exc:
        raise _err(exc) from exc
    return {"ok": True, "installed": {k: row[k] for k in ("id", "kind", "name", "version", "enabled", "installed_at")}, "active_theme": market_service.active_theme_id(user["id"])}


@router.post("/uninstall")
def uninstall(body: PluginRef, request: Request) -> dict:
    user = deps.current_user(request)
    try:
        _locked(market_service.uninstall)(user["id"], body.id)
    except _Busy:
        raise HTTPException(status_code=409, detail={"message": "市场正在处理另一个变更，请稍后重试"}) from None
    except market_service.MarketError as exc:
        raise _err(exc) from exc
    return {"ok": True, "active_theme": market_service.active_theme_id(user["id"])}


@router.post("/toggle")
def toggle(body: ToggleBody, request: Request) -> dict:
    user = deps.current_user(request)
    try:
        row = _locked(market_service.set_enabled)(user["id"], body.id, body.enabled)
    except _Busy:
        raise HTTPException(status_code=409, detail={"message": "市场正在处理另一个变更，请稍后重试"}) from None
    except market_service.MarketError as exc:
        raise _err(exc) from exc
    return {"ok": True, "installed": {k: row[k] for k in ("id", "kind", "name", "version", "enabled")}, "active_theme": market_service.active_theme_id(user["id"])}


@router.post("/undo")
def undo(request: Request) -> dict:
    user = deps.current_user(request)
    try:
        msg = _locked(market_service.undo_last)(user["id"])
    except _Busy:
        raise HTTPException(status_code=409, detail={"message": "市场正在处理另一个变更，请稍后重试"}) from None
    except market_service.MarketError as exc:
        raise _err(exc) from exc
    return {"ok": True, "message": msg, "active_theme": market_service.active_theme_id(user["id"])}
