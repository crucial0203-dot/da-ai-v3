"""V4.0 N6 工具目录路由。

对外只暴露「有哪些工具、参数是什么、要不要批准」——
执行由对话回路与工作流引擎内部调用，不开公网 HTTP 入口给模型直连。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import db, deps
from ..services.tools import executor, registry

router = APIRouter(prefix="/api/tools", tags=["tools"])


class ToolIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    requires_approval: bool = False


@router.get("")
def list_tools(request: Request):
    """当前用户可用的工具列表。

    数据库里的行 + 内存注册表里的插件/市场工具——后者从不落表（只在
    PLUGIN_SPECS 里），不在这里补上，工具页就看不见刚装的工具包。
    """
    user = deps.current_user(request)
    # 市场工具按用户门禁（与 executor.enabled_tools 同一套口径）
    market_names: set[str] = set()
    gate_on: set[str] = set()
    try:
        from ..services import market

        market_names = market.market_tool_names()
        gate_on = market.active_tool_names(user["id"])
    except Exception:  # noqa: BLE001
        pass
    rows = db.rows(
        "SELECT name, label, desc, schema_json, enabled, requires_approval, builtin FROM tools "
        "ORDER BY builtin DESC, name"
    )
    tools = []
    for row in rows:
        spec = registry.tool_spec(row["name"])
        tools.append({
            "name": row["name"],
            "label": row["label"],
            "desc": row["desc"],
            "schema": json.loads(row["schema_json"] or "{}"),
            "enabled": bool(row["enabled"]),
            "requires_approval": bool(row["requires_approval"]),
            "builtin": bool(row["builtin"]),
            "conversational": bool(spec.conversational) if spec else False,
            "source": "builtin",
        })
    seen = {t["name"] for t in tools}
    for name, spec in registry.PLUGIN_SPECS.items():
        if name in seen:
            continue
        tools.append({
            "name": spec.name,
            "label": spec.label,
            "desc": spec.desc,
            "schema": spec.schema,
            # 插件工具没有数据库开关：有 handler 且（非市场工具 或 本人启用了）才算启用
            "enabled": name in executor._HANDLERS and (name not in market_names or name in gate_on),
            "requires_approval": bool(spec.requires_approval),
            "builtin": False,
            "conversational": bool(spec.conversational),
            "source": "market",
        })
    return {"tools": tools, "allowed": sorted(registry.ALLOWED_TOOLS)}


@router.patch("/{tool_name}")
def update_tool(tool_name: str, payload: ToolIn, request: Request):
    """启用/停用工具，或调整是否需要审批。

    平台禁止的工具（FORBIDDEN）不能被启用。
    """
    user = deps.require_admin(request)
    if tool_name in registry.FORBIDDEN_TOOLS:
        raise HTTPException(status_code=400, detail=f"工具 {tool_name} 被平台禁止，不能启用")
    row = db.one("SELECT name FROM tools WHERE name = ?", (tool_name,))
    if not row:
        raise HTTPException(status_code=404, detail="工具不存在")
    db.execute(
        "UPDATE tools SET enabled = ?, requires_approval = ? WHERE name = ?",
        (1 if payload.enabled else 0, 1 if payload.requires_approval else 0, tool_name),
    )
    return {"ok": True}


@router.get("/gates")
def list_gates(request: Request):
    """逻辑门真值表（前端画布的属性面板要用）。"""
    deps.current_user(request)
    from ..services.workflow.gates import gate_truth_table

    return {"gates": gate_truth_table()}
