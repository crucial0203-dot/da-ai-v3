"""S1 批次（正确性与安全）单元测试：owner 门禁 / 审批 fail-closed / 安装事务 /
resume 指纹 / 检索转义与兜底排序。

运行（无需起服务，全部走临时 DATA_DIR + 服务函数直调）：
  cd da-ai-v3 && ./.venv/Scripts/python.exe tests/m30_s1_batch.py

对应调研报告 `outputs/开源源码调研-2026-10-25/00-总览与落地路线图.md` 批次 1：
  S1-1 工具 owner 校验（deepseek-harness scope 分层）
  S1-2 审批 fail-closed + needs_approval 钩子（mastra）
  S1-3 安装事务 + 意图日志对账 + 幽灵注册清扫 + 空包拒装（dsh-market）
  S1-4 resume 指纹防陈旧提交（agent-flow executionFingerprint）
  S1-5 FTS5 查询转义 + BM25 兜底粗筛排序（dify + 本轮巡检）
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 隔离数据目录：必须在 import app.config 之前设置
_TMP = tempfile.mkdtemp(prefix="m30-")
os.environ["DATA_DIR"] = _TMP

from app import config, db  # noqa: E402
from app.services import market, plugins  # noqa: E402
from app.services.rag import retriever  # noqa: E402
from app.services.tools import executor, registry  # noqa: E402
from app.services.workflow import engine  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"{'  OK ' if ok else '  !! '} {name}" + (f"  [{detail}]" if detail else ""))
    return ok


_BASE_HANDLERS: dict | None = None


def reset_db() -> None:
    """重建临时库，清掉模块级缓存，让每个用例从干净状态开始。"""
    global _BASE_HANDLERS
    config.DATA_DIR = Path(_TMP)
    config.DB_PATH = config.DATA_DIR / "app.db"
    config.ensure_dirs()
    db._COLUMN_CACHE.clear()
    db._journal = None
    db.init_db()
    market._catalog_cache = None
    market._active_toolpacks.clear()
    # 内置 handler 不能靠「清空后重新 import」恢复：builtin 包此时已在
    # sys.modules 里，二次 import 是缓存命中，不会重跑 @register。
    # 取一次基线快照，之后每个用例恢复到它。
    if _BASE_HANDLERS is None:
        executor._ensure_handlers()
        _BASE_HANDLERS = dict(executor._HANDLERS)
    else:
        executor._HANDLERS.clear()
        executor._HANDLERS.update(_BASE_HANDLERS)


TOOL_PACK = "tool.calc"
TOOL_NAME = "calc"


def _install_pack(user_id: str) -> None:
    market.install(user_id, TOOL_PACK)


# ── S1-1 市场工具 owner 门禁 ─────────────────────────────

def t_owner_gate() -> None:
    print("\n== S1-1 市场工具 owner 门禁 ==")
    reset_db()
    _install_pack("u_owner")
    # A 用户（装了）能调
    res_a = asyncio.run(executor.execute_tool(
        TOOL_NAME, {"expression": "1+1"}, {"user_id": "u_owner"}
    ))
    check("装了工具包的用户可以执行",
          res_a.ok and isinstance(res_a.output, dict) and res_a.output.get("result") == 2,
          f"ok={res_a.ok} out={res_a.output!r} err={res_a.error}")
    # B 用户（没装）按名调 → 拒
    res_b = asyncio.run(executor.execute_tool(
        TOOL_NAME, {"expression": "1+1"}, {"user_id": "u_other"}
    ))
    check("没装的用户执行被拒（越权路径已关闭）",
          (not res_b.ok) and ("未安装" in res_b.error or "停用" in res_b.error), res_b.error)
    # 没有 user_id 的上下文 → 拒（fail closed）
    res_c = asyncio.run(executor.execute_tool(
        TOOL_NAME, {"expression": "1+1"}, {"task_id": "t1"}
    ))
    check("缺少用户上下文时拒绝市场工具", not res_c.ok, res_c.error)
    # 停用后同用户也调不动（_sync_toolpack 会把它从白名单摘掉，
    # 所以在白名单层就被拒——「不在平台白名单内」也是正确的拒绝）
    market.set_enabled("u_owner", TOOL_PACK, False)
    res_d = asyncio.run(executor.execute_tool(
        TOOL_NAME, {"expression": "1+1"}, {"user_id": "u_owner"}
    ))
    check("停用后本用户也调不动", not res_d.ok, res_d.error)
    # 内置工具不受这道门影响
    res_e = asyncio.run(executor.execute_tool(
        "memory_query", {"query": "x"}, {"user_id": "u_nobody"}
    ))
    check("内置工具不受市场门禁影响", res_e.ok, res_e.error)


# ── S1-2 审批 fail-closed ────────────────────────────────

def t_approval() -> None:
    print("\n== S1-2 审批 fail-closed 与钩子 ==")
    reset_db()

    def boom(args, ctx):
        raise RuntimeError("策略炸了")

    spec = registry.ToolSpec("t_boom", "t", "d", needs_approval=boom)
    check("needs_approval 抛异常时按需要审批处理（fail closed）",
          executor._approval_required(spec, {}, {}) is True)

    def only_pptx(args, ctx):
        return str(args.get("doc_type") or "") == "pptx"

    spec2 = registry.ToolSpec("t_ppt", "t", "d", needs_approval=only_pptx)
    check("钩子按参数动态判定（pptx 要批）",
          executor._approval_required(spec2, {"doc_type": "pptx"}, {}) is True)
    check("钩子按参数动态判定（docx 免批）",
          executor._approval_required(spec2, {"doc_type": "docx"}, {}) is False)

    spec3 = registry.ToolSpec("t_exempt", "t", "d", requires_approval=True, approval_exempt=True)
    check("审批豁免标记优先于静态 requires_approval",
          executor._approval_required(spec3, {}, {}) is False)

    builtin = registry.tool_spec("doc_generate")
    check("内置 doc_generate 仍需审批（语义不回退）",
          builtin is not None and executor._approval_required(builtin, {}, {}) is True)

    # 端到端：钩子说不要批 → 直接执行；说批 → needs_approval 回给上层
    executor._HANDLERS["t_hook"] = lambda ctx, args: _async_ok(args)
    spec4 = registry.ToolSpec("t_hook", "t", "d", needs_approval=lambda a, c: False)
    registry.register_plugin_tool(spec4)
    r1 = asyncio.run(executor.execute_tool("t_hook", {}, {"user_id": "u"}))
    check("钩子放行时工具直接执行", r1.ok, r1.error)
    registry.unregister_plugin_tool("t_hook")
    executor._HANDLERS.pop("t_hook", None)


async def _async_ok(args):
    return {"echo": args}


# ── S1-3 安装事务 / 意图日志 / 幽灵清扫 ──────────────────

def t_install_tx() -> None:
    print("\n== S1-3 安装事务、意图日志、对账 ==")
    reset_db()
    _install_pack("u_tx")
    row = db.one("SELECT * FROM market_plugins WHERE user_id = ? AND id = ?", ("u_tx", TOOL_PACK))
    check("安装后 DB 有行", row is not None and row["enabled"] == 1)
    check("安装后工具注册进 PLUGIN_SPECS", TOOL_NAME in registry.PLUGIN_SPECS)
    check("安装后工具进 ALLOWED_TOOLS", TOOL_NAME in registry.ALLOWED_TOOLS)
    j = db.one("SELECT * FROM market_journal WHERE user_id = ? AND plugin_id = ? ORDER BY id DESC", ("u_tx", TOOL_PACK))
    check("意图日志已闭环（begin→done）", j is not None and j["state"] == "done" and j["finished_at"])
    ops = db.rows("SELECT * FROM market_ops WHERE user_id = ? AND plugin_id = ?", ("u_tx", TOOL_PACK))
    check("审计记录了 install", len(ops) == 1 and ops[0]["op"] == "install")

    # 卸载：行、注册、日志全部干净
    market.uninstall("u_tx", TOOL_PACK)
    gone = db.one("SELECT 1 FROM market_plugins WHERE user_id = ? AND id = ?", ("u_tx", TOOL_PACK))
    check("卸载后 DB 行没了", gone is None)
    check("卸载后 PLUGIN_SPECS 没了", TOOL_NAME not in registry.PLUGIN_SPECS)
    check("卸载后 ALLOWED_TOOLS 没了", TOOL_NAME not in registry.ALLOWED_TOOLS)
    check("卸载后 handler 没了", TOOL_NAME not in executor._HANDLERS)
    j2 = db.one("SELECT * FROM market_journal WHERE user_id = ? AND op = 'uninstall' ORDER BY id DESC", ("u_tx",))
    check("卸载的意图日志也闭环", j2 is not None and j2["state"] == "done")


def t_sweep() -> None:
    print("\n== S1-3 启动对账（崩溃残留收敛） ==")
    reset_db()
    # 场景 1：install 写到一半被杀（DB 无行、日志停在 begin）→ 当没装过。
    # 用一个从未安装过的插件 id，否则它会被场景 2 的安装「洗白」。
    db.execute(
        "INSERT INTO market_journal (user_id, op, plugin_id, state, created_at) VALUES (?,?,?, 'begin', ?)",
        ("u_sw", "install", "tool.never-installed", db.now_iso()),
    )
    # 场景 2：uninstall 写到一半被杀（DB 有行、日志停在 begin）→ 反向删干净
    _install_pack("u_sw")
    db.execute(
        "INSERT INTO market_journal (user_id, op, plugin_id, state, created_at) VALUES (?,?,?, 'begin', ?)",
        ("u_sw", "uninstall", TOOL_PACK, db.now_iso()),
    )
    # 场景 3：DB 已无记录但进程里还活着（幽灵注册）
    ghost = plugins.PluginContext(plugins.PluginInfo(id="market.ghost", name="g", version="1"))
    ghost.register_tool(registry.ToolSpec("ghost_tool", "g", "d"), _async_ok)
    market._active_toolpacks["ghost"] = ghost

    fixed = market.sweep_market_journal()
    check("对账收敛了未完成的 install", fixed["install_dropped"] == 1, str(fixed))
    check("对账收敛了未完成的 uninstall", fixed["uninstall_dropped"] == 1, str(fixed))
    # 两个：场景 2 半截卸载的 tool.calc + 场景 3 的 ghost
    check("对账清掉了幽灵注册", fixed["tools_cleared"] == 2, str(fixed))
    left = db.one("SELECT 1 FROM market_plugins WHERE user_id = ? AND id = ?", ("u_sw", TOOL_PACK))
    check("uninstall 半截状态已被反向删除", left is None)
    check("幽灵工具已从注册表摘掉", "ghost_tool" not in registry.PLUGIN_SPECS)
    undone = db.rows("SELECT * FROM market_journal WHERE state = 'begin'")
    check("对账后没有残留的 begin", not undone)

    # 对账幂等：再跑一次不动任何东西
    fixed2 = market.sweep_market_journal()
    check("对账幂等（二次无副作用）", not any(fixed2.values()), str(fixed2))


def t_rollback_and_empty_pack() -> None:
    print("\n== S1-3 整包回滚与空包拒装 ==")
    reset_db()
    # 整包回滚：第一个工具注册成功，第二个撞名 → 两个都不留
    ctx = plugins.PluginContext(plugins.PluginInfo(id="p.rollback", name="r", version="1"))
    ctx.register_tool(registry.ToolSpec("fresh_tool_a", "a", "d"), _async_ok)
    check("第一个工具注册成功", "fresh_tool_a" in registry.PLUGIN_SPECS)
    try:
        ctx.register_tool(registry.ToolSpec("web_search", "dup", "d"), _async_ok)
        raised = False
    except ValueError:
        raised = True
    check("第二个工具撞名抛错", raised)
    check("整包回滚：第一个工具也被撤销", "fresh_tool_a" not in registry.PLUGIN_SPECS)
    check("整包回滚：ALLOWED_TOOLS 无残留", "fresh_tool_a" not in registry.ALLOWED_TOOLS)
    check("整包回滚：handler 无残留", "fresh_tool_a" not in executor._HANDLERS)
    check("整包回滚：ctx.tools 已清空", ctx.tools == [])
    # dispose 不因回滚过的 undo 二次报错
    ctx.dispose()

    # 空包拒装：目录条目所有工具名都被占用 → install 整体失败且不留行
    real_payload = market.entry_payload
    market.entry_payload = lambda entry: {
        "tools": [{"name": "web_search", "handler": {"kind": "echo"}, "schema": {"type": "object", "properties": {}}}]
    }
    try:
        market.install("u_empty", TOOL_PACK)
        raised = False
    except market.MarketError as exc:
        raised = True
        msg = exc.message
    finally:
        market.entry_payload = real_payload
    check("空包安装被拒（MarketError）", raised, msg if raised else "")
    row = db.one("SELECT 1 FROM market_plugins WHERE user_id = ? AND id = ?", ("u_empty", TOOL_PACK))
    check("空包拒装后 DB 无残留行", row is None)
    ops = db.rows("SELECT * FROM market_ops WHERE user_id = ?", ("u_empty",))
    check("空包拒装后无审计残留（事务回滚）", not ops)
    check("空包拒装后注册表干净", "web_search" not in market._active_toolpacks)


# ── S1-4 resume 指纹 ─────────────────────────────────────

def t_fingerprint() -> None:
    print("\n== S1-4 resume 指纹防陈旧提交 ==")
    reset_db()
    graph = {
        "nodes": [
            {"id": "n1", "type": "start", "data": {}},
            {"id": "n2", "type": "llm", "data": {"prompt": "写点什么"}},
            {"id": "n3", "type": "output", "data": {"text": "{{n2}}"}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3"},
        ],
    }
    moved = json.loads(json.dumps(graph))
    for n in moved["nodes"]:
        n["position"] = {"x": 999, "y": 999}
    reordered = json.loads(json.dumps(graph))
    reordered["nodes"].reverse()
    changed = json.loads(json.dumps(graph))
    changed["nodes"][1]["data"]["prompt"] = "改成别的提示词"

    fp = engine.graph_fingerprint(graph)
    check("指纹对节点顺序不敏感", fp == engine.graph_fingerprint(reordered))
    check("指纹对画布位置不敏感（布局≠逻辑）", fp == engine.graph_fingerprint(moved))
    check("指纹对执行语义敏感（改了 prompt 就变）", fp != engine.graph_fingerprint(changed))

    # run 落库存指纹；改图后 resume 被引擎拒（stale）
    db.execute(
        "INSERT INTO workflow_runs(id, workflow_id, user_id, status, input_json, created_at, updated_at, graph_hash) "
        "VALUES('wfr_x','wf_x','u_fp','waiting_approval','{}',?,?,?)",
        (db.now_iso(), db.now_iso(), fp),
    )
    events = asyncio.run(_drain(engine.run_workflow(
        "wf_x", "u_fp", changed, run_id="wfr_x", resume_outputs={},
    )))
    check("图被改过后续跑被拒（stale）",
          bool(events) and events[0].kind == "error" and events[0].status == "stale",
          f"{events[0].kind}/{events[0].status}" if events else "无事件")
    check("stale 事件带原因", bool(events) and "重新运行" in str(events[0].data.get("message", "")))
    # 同一张图 resume 不被拒（不会误伤）
    events2 = asyncio.run(_drain(engine.run_workflow(
        "wf_x", "u_fp", graph, run_id="wfr_x", resume_outputs={},
    )))
    check("图未变时不误拒（首事件不是 stale error）",
          bool(events2) and not (events2[0].kind == "error" and events2[0].status == "stale"),
          f"{events2[0].kind}/{events2[0].status}" if events2 else "无事件")


async def _drain(agen) -> list:
    out = []
    async for ev in agen:
        out.append(ev)
        if ev.kind in {"done", "error"}:
            break
    return out


# ── S1-5 检索转义与兜底排序 ──────────────────────────────

def t_retrieval() -> None:
    print("\n== S1-5 FTS5 转义与 BM25 兜底排序 ==")
    reset_db()
    check("FTS5 字符串字面量转义", retriever._fts_quote('a"b') == '"a""b"', retriever._fts_quote('a"b'))
    check("LIKE 通配符转义", retriever._like_escape("a_b%c") == "a\\_b\\%c", retriever._like_escape("a_b%c"))

    # 造一个集合：600 块，只有第 501 块之后含关键词「行政协议」
    uid = "u_rag"
    db.execute(
        "INSERT INTO rag_collections(id, user_id, name, created_at) VALUES(?,?,?,?)",
        ("col_1", uid, "测试集合", db.now_iso()),
    )
    db.execute(
        "INSERT INTO rag_documents(id, collection_id, name, chars, chunk_count, status, created_at) "
        "VALUES(?,?,?,?,600,'ready',?)",
        ("doc_1", "col_1", "长文档.txt", 6000, db.now_iso()),
    )
    with db.tx() as conn:
        for i in range(600):
            body = f"第 {i} 块：无关内容" * 3 if i < 500 else f"第 {i} 块：行政协议的司法解释"
            conn.execute(
                "INSERT INTO rag_chunks(doc_id, collection_id, seq, heading, body, created_at) "
                "VALUES(?,?,?,?,?,?)",
                ("doc_1", "col_1", i, f"第{i}节", body, db.now_iso()),
            )

    # 关掉 FTS5 走 BM25 兜底：原实现 LIMIT 500 无排序，第 501 块永远扫不到
    real_fts = db.fts5_enabled
    db.fts5_enabled = lambda: False
    try:
        hits = retriever.search(["col_1"], uid, "行政协议", top_k=5)
    finally:
        db.fts5_enabled = real_fts
    check("BM25 兜底能命中 LIMIT 500 之后的块（原缺陷回归）",
          bool(hits) and any("行政协议" in h["body"] for h in hits),
          f"hits={len(hits)}")

    # 带引号的查询不再让 FTS5 语法错误后静默返回空
    try:
        hits2 = retriever.search(["col_1"], uid, '行政"协议', top_k=5)
        ok = isinstance(hits2, list)
    except Exception as exc:  # noqa: BLE001
        ok = False
        print("   ", type(exc).__name__, exc)
    check("带引号查询不抛异常（转义生效）", ok)


def main() -> int:
    t_owner_gate()
    t_approval()
    t_install_tx()
    t_sweep()
    t_rollback_and_empty_pack()
    t_fingerprint()
    t_retrieval()
    print(f"\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print("  -", name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
