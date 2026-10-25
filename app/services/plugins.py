"""后端插件系统 v0.1 —— 借鉴 deepseek-harness「Everything is a Plugin」的可逆注册设计。

约定：`app/plugins/` 下每个模块暴露两样东西——

    PLUGIN: PluginInfo          # 身份声明（id/name/version）
    def activate(ctx) -> ...    # 向 ctx 注册工具/服务/钩子，返回 disposer（可选）

一切注册可逆：插件卸载（deactivate）时按相反顺序撤销，不留残留。
工具注册走 tools.registry 的插件扩展区，FORBIDDEN_TOOLS 仍然硬禁止。
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .tools import executor as tools_executor
from .tools import registry as tools_registry
from .tools.registry import ToolSpec


@dataclass(frozen=True)
class PluginInfo:
    id: str
    name: str
    version: str
    description: str = ""
    ui_slots: tuple[str, ...] = ()   # 前端同 id 插件贡献的槽位（声明用）


def _run_once(fn: Callable[[], None]) -> Callable[[], None]:
    """包成只跑一次：整包回滚执行过的 undo，dispose 时再调也不会二次生效。"""
    state = {"done": False}

    def wrapped() -> None:
        if state["done"]:
            return
        state["done"] = True
        fn()

    return wrapped


class PluginContext:
    """插件拿到的注册面。每类注册都返回/登记 disposer。"""

    def __init__(self, info: PluginInfo):
        self.info = info
        self._disposers: list[Callable[[], None]] = []
        self._tool_undos: list[Callable[[], None]] = []
        self.services: dict[str, Any] = {}
        self.tools: list[str] = []
        self.hooks: dict[str, list[str]] = {}
        self._events: dict[str, list[Callable[..., Awaitable[None] | None]]] = {}

    # ---- 工具 ----
    def register_tool(self, spec: ToolSpec, handler: Callable[[dict, dict], Awaitable[Any]]) -> None:
        try:
            tools_registry.register_plugin_tool(spec)
            undo_handler = tools_executor.register_handler(spec.name, handler)
        except Exception:
            # 整包原子：这个工具注册失败时，把本插件本轮已注册的工具全部撤掉。
            # 否则「第 2 个工具撞名抛 ValueError、第 1 个工具的 disposer 随 ctx
            # 一起被丢弃」会往 PLUGIN_SPECS/ALLOWED_TOOLS/_HANDLERS 留下幽灵
            # 注册，且下次启动 boot_installed 还会把它们当正常工具恢复
            # （2026-10-25 修）。
            self._rollback_tools()
            raise

        def undo() -> None:
            undo_handler()
            tools_registry.unregister_plugin_tool(spec.name)

        once = _run_once(undo)
        self._disposers.append(once)
        self._tool_undos.append(once)
        self.tools.append(spec.name)

    def _rollback_tools(self) -> None:
        """撤销本插件已注册的工具（注册中途失败时用）。幂等。"""
        for undo in reversed(self._tool_undos):
            try:
                undo()
            except Exception:  # noqa: BLE001
                pass
        self._tool_undos.clear()
        self.tools.clear()

    # ---- 服务（插件间按 key 解耦查找，对标 Cordis ctx.<key>）----
    def register_service(self, key: str, svc: Any) -> None:
        self.services[key] = svc
        self._disposers.append(lambda: self.services.pop(key, None))

    # ---- 事件钩子（轻量版：tool.pre_execute / tool.post_execute 等）----
    def on(self, event: str, handler: Callable[..., Awaitable[None] | None]) -> None:
        self._events.setdefault(event, []).append(handler)
        self.hooks.setdefault(event, []).append(getattr(handler, "__name__", "handler"))

        def undo() -> None:
            lst = self._events.get(event) or []
            if handler in lst:
                lst.remove(handler)

        self._disposers.append(undo)

    async def emit(self, event: str, payload: dict) -> None:
        for handler in self._events.get(event, []):
            result = handler(payload)
            if hasattr(result, "__await__"):
                await result

    def dispose(self) -> None:
        for undo in reversed(self._disposers):
            try:
                undo()
            except Exception:  # noqa: BLE001 —— 卸载失败不能拖垮其他插件
                pass
        self._disposers.clear()


@dataclass
class LoadedPlugin:
    info: PluginInfo
    ctx: PluginContext
    status: str = "active"          # active | failed
    error: str = ""


LOADED: dict[str, LoadedPlugin] = {}


def load_builtin_plugins() -> list[LoadedPlugin]:
    """扫描并激活 app/plugins/ 下的插件模块（幂等）。"""
    if LOADED:
        return list(LOADED.values())
    from .. import plugins as plugins_pkg  # app/plugins 包

    for mod_info in pkgutil.iter_modules(plugins_pkg.__path__):
        if mod_info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"app.plugins.{mod_info.name}")
            info: PluginInfo = getattr(mod, "PLUGIN")
            ctx = PluginContext(info)
            maybe = mod.activate(ctx)
            if callable(maybe):
                ctx._disposers.append(maybe)
            LOADED[info.id] = LoadedPlugin(info=info, ctx=ctx)
        except Exception as err:  # noqa: BLE001 —— 单插件失败不能拖垮启动
            LOADED[mod_info.name] = LoadedPlugin(
                info=PluginInfo(id=mod_info.name, name=mod_info.name, version="?"),
                ctx=PluginContext(PluginInfo(id=mod_info.name, name=mod_info.name, version="?")),
                status="failed",
                error=str(err),
            )
    return list(LOADED.values())


def deactivate(plugin_id: str) -> bool:
    rec = LOADED.pop(plugin_id, None)
    if not rec:
        return False
    rec.ctx.dispose()
    return True


def public_list() -> list[dict]:
    """/api/plugins 数据源。"""
    return [
        {
            "id": rec.info.id,
            "name": rec.info.name,
            "version": rec.info.version,
            "description": rec.info.description,
            "status": rec.status,
            **({"error": rec.error} if rec.error else {}),
            "provides": {
                "tools": rec.ctx.tools,
                "services": sorted(rec.ctx.services),
                "hooks": rec.ctx.hooks,
                "ui_slots": list(rec.info.ui_slots),
            },
        }
        for rec in LOADED.values()
    ]


async def emit(event: str, payload: dict) -> None:
    """向所有插件广播事件（chat 回路后续接上）。"""
    for rec in list(LOADED.values()):
        if rec.status == "active":
            await rec.ctx.emit(event, payload)
