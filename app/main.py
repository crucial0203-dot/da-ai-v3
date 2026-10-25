"""应用装配。

启动顺序刻意压到最短：建表、必要时初始化管理员、立刻开始监听。
依赖在 install 阶段装好，start 只 import，保证 60 秒就绪窗口内稳定起来。
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from . import config, db, security
from .services import market as market_service
from .services import plugins as plugins_service
from .routers import (
    admin,
    admin_skills,
    artifacts,
    auth,
    chat,
    files,
    generate,
    health,
    market,
    memory,
    models,
    rag,
    skills,
    tasks,
    tools,
    workflows,
)


def seed_invite() -> None:
    """首启没有任何邀请码时种一个，并把码打进启动日志——否则没人进得来。"""
    existing = db.one("SELECT COUNT(*) AS n FROM invites")["n"]
    if existing:
        return
    code = security.new_invite_code()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO invites(code, max_uses, used, days, daily_quota, expires_at, note, created_at) "
            "VALUES(?,?,0,7,50,?,?,?)",
            (code, 5, (db.datetime.now(db.TZ) + db.timedelta(days=7)).isoformat(timespec="seconds"),
             "初始邀请码", db.now_iso()),
        )
    db.set_setting("invite_seeded", "1")
    print(f"[bootstrap] 首个邀请码 {code}（5 人 / 每人每天 50 次 / 7 天），到 /admin 可以再建", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    db.init_db()
    seed_invite()
    loaded = plugins_service.load_builtin_plugins()
    ok = sum(1 for p in loaded if p.status == "active")
    users = [r["user_id"] for r in db.rows("SELECT DISTINCT user_id FROM market_plugins")]
    # 先对账再恢复：未 finish 的意图按 DB 真相收敛，并清掉崩溃留下的幽灵注册。
    # 顺序不能反——先 boot_installed 会把本该清掉的包又注册回来。
    swept = market_service.sweep_market_journal()
    market_ok = market_service.boot_installed(users)
    print(
        f"[boot] {config.APP_NAME} data_dir={config.DATA_DIR} model={config.MODEL} "
        f"key={'yes' if config.MINIMAX_API_KEY else 'no'} journal={db.journal_mode()} "
        f"quota={config.DAILY_CALL_QUOTA}/日 plugins={ok}/{len(loaded)} market_tools={market_ok} "
        f"journal_swept={swept}",
        flush=True,
    )
    yield


app = FastAPI(title=config.APP_NAME, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class SecurityMiddleware(BaseHTTPMiddleware):
    """同源校验 + 安全响应头。不带 CORS 头，也不允许跨站写操作。"""

    @staticmethod
    def _host_set(request: Request) -> set[str]:
        """反代会改写 Host，也可能追加 X-Forwarded-Host，两者都要认；端口一并去掉。"""
        hosts: set[str] = set()
        for name in ("host", "x-forwarded-host", "x-original-host"):
            raw = request.headers.get(name)
            if not raw:
                continue
            for part in raw.split(","):
                value = part.strip().lower()
                if not value:
                    continue
                hosts.add(value)
                hosts.add(value.split(":")[0])
        return hosts

    @classmethod
    def _same_origin(cls, request: Request, origin: str) -> bool:
        if config.PUBLIC_ORIGIN and origin.rstrip("/").lower() == config.PUBLIC_ORIGIN.lower():
            return True
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"}:
            return False
        netloc = (parsed.netloc or "").lower()
        if not netloc:
            return False
        hosts = cls._host_set(request)
        if not hosts:
            # 连 Host 都没有（几乎不可能），交给 SameSite Cookie 兜底，不误杀真实用户
            return True
        return netloc in hosts or netloc.split(":")[0] in hosts

    async def dispatch(self, request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and not self._same_origin(request, origin):
                return JSONResponse(
                    {"detail": "跨站请求被拒绝", "hint": f"origin={origin} host={request.headers.get('host', '')}"},
                    status_code=403,
                )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            # script-src 放行 blob:：插件市场运行时用 Blob URL 动态加载 UI 模块
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' blob:; connect-src 'self'; font-src 'self'",
        )
        return response


app.add_middleware(SecurityMiddleware)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """detail 允许是 dict，用来带 code 让前端做分支判断（比如"看不了图片"要引导换模型）。

    老代码全部用字符串 detail，这条分支保持响应体与以前完全一致：
    `{"detail": "..."}`，前端 api.js 照旧当作消息显示。
    """
    detail = exc.detail
    if isinstance(detail, dict):
        body = {"detail": detail.get("message") or "请求失败"}
        for key in ("code", "model", "hint", "quota", "used"):
            if key in detail:
                body[key] = detail[key]
    else:
        body = {"detail": detail}
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


app.include_router(auth.router)
app.include_router(tasks.router)
app.include_router(chat.router)
app.include_router(files.router)
app.include_router(generate.router)
app.include_router(skills.router)
app.include_router(admin.router)
app.include_router(admin_skills.router)
app.include_router(market.router)
app.include_router(health.router)
app.include_router(models.router)
app.include_router(rag.router)
app.include_router(tools.router)
app.include_router(memory.router)
app.include_router(artifacts.router)
app.include_router(workflows.router)


@app.get("/api/usage")
def usage_series(request: Request):
    from . import deps

    visitor = deps.current_visitor(request)
    rows = db.rows(
        "SELECT day, in_tokens, out_tokens, cache_read, calls FROM usage_daily "
        "WHERE visitor_id = ? ORDER BY day DESC LIMIT 14",
        (visitor["id"],),
    )
    return {
        "days": [dict(r) for r in reversed(rows)],
        "today": deps.usage_today(visitor["id"]),
        "quota": deps.quota_detail(visitor),
    }


@app.get("/api/plugins")
def plugins_list():
    """已加载后端插件清单（内置 + 市场工具包；工具/服务/钩子）。"""
    plugins = plugins_service.public_list()
    for snap in market_service.market_tools_snapshot():
        plugins.append({
            "id": snap["id"],
            "name": snap["id"].removeprefix("market."),
            "version": "market",
            "description": "插件市场安装的工具包",
            "status": "active",
            "provides": {"tools": snap["tools"], "services": [], "hooks": {}, "ui_slots": []},
        })
    return {"plugins": plugins}


@app.get("/api/meta")
def meta():
    from .services import capabilities, models, web_search

    return {
        "app": config.APP_NAME,
        # 旧键保留：老前端拿它显示"当前模型"，新前端读 models 列表
        "model": config.MODEL,
        # 老前端不认识 available 字段，这里只列 provider 有 Key 的模型；
        # 全量清单（含未配置 provider 的预填）走 /api/models，新前端用
        "models": [m for m in models.public_list() if m["available"]],
        "default_model": models.default_model_id(),
        "efforts": list(models.EFFORTS),
        "effort_labels": models.EFFORT_LABELS,
        "max_upload_mb": config.MAX_UPLOAD_BYTES // (1024 * 1024),
        "upload": {
            "ext": sorted(config.ALLOWED_UPLOAD_EXT),
            "image_ext": sorted(config.IMAGE_EXT),
            "max_mb": config.MAX_UPLOAD_BYTES // (1024 * 1024),
            "max_image_mb": config.MAX_IMAGE_UPLOAD_BYTES // (1024 * 1024),
            "max_files_per_batch": config.MAX_BATCH_FILES,
            "max_images_per_task": config.MAX_IMAGES_PER_TASK,
            "max_images_per_turn": config.MAX_IMAGES_PER_TURN,
            "vision_enabled": config.VISION_ENABLED,
        },
        # 兜底额度：码没设额度/迁移降级时才生效；个人真实额度在 /api/me 的 quota
        "daily_calls": config.DAILY_CALL_QUOTA,
        "quota_fallback": config.DAILY_CALL_QUOTA,
        "retain_days": config.FILE_RETAIN_DAYS,
        "modes": [
            {"key": "chat", "label": "对话", "desc": "普通问答、头脑风暴，不走工作流"},
            {"key": "research", "label": "读材料", "desc": "论文、判决、法规的拆解与定位"},
            {"key": "writing", "label": "写文章", "desc": "读书笔记、读后感、综述、论文段落"},
            {"key": "slides", "label": "做演示", "desc": "汇报结构、逐页内容、演讲备注"},
            {"key": "docs", "label": "整文书", "desc": "报告、说明、纪要、正式文本排版"},
        ],
        "capabilities": capabilities.public_matrix(),
        # 联网搜索：三档源的可用状态，前端据此显示开关与降级提示
        "web": {
            "enabled": config.WEB_SEARCH_ENABLED,
            "top_k": config.WEB_SEARCH_TOP_K,
            "providers": [
                {"id": p["id"], "label": p["label"], "available": p["available"],
                 "degraded": p["degraded"], "note": p["note"]}
                for p in web_search.available_providers()
            ],
        },
        # 轻量 RAG：集合列表由 /api/rag/collections 提供，这里只报开关与引擎
        "rag": {
            "enabled": config.RAG_ENABLED,
            "fts5": db.fts5_enabled(),
            "top_k": config.RAG_TOP_K,
        },
    }


STATIC = config.STATIC_DIR
DIST = STATIC / "ui"


def _index_response() -> FileResponse:
    """首页响应：React 构建产物（V4.0 N2 起唯一前端）。

    index.html 内容随发布变化（构建产物 hash 文件名会换），必须 no-cache，
    否则用户浏览器可能拿着旧 html 去引已不存在的 assets。
    """
    target = DIST / "index.html"
    resp = FileResponse(target)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
def index():
    return _index_response()


@app.get("/{full_path:path}")
def spa(full_path: str):
    if full_path.startswith(("api/", "__")):
        raise HTTPException(status_code=404, detail="Not Found")
    candidate = (STATIC / full_path).resolve()
    if STATIC.resolve() in candidate.parents and candidate.is_file():
        # 构建产物文件名带内容 hash，可以放心长缓存；带 hash 是前提，别放宽到别的目录
        if full_path.startswith("ui/bundle/"):
            resp = FileResponse(candidate)
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return resp
        return FileResponse(candidate)
    return _index_response()
