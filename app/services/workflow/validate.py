"""工作流图校验（服务端强校验）。

保存前必须过这一关。错误按 node id 归集，前端在对应节点上打红点。

图结构（与 @xyflow/react 的 Node/Edge 对应）：
```json
{
  "nodes": [{"id":"n1","type":"llm","position":{"x":0,"y":0},
             "data":{"label":"写正文","prompt":"...","model":"MiniMax-M3"}}],
  "edges": [{"id":"e1","source":"n1","target":"n2"}]
}
```
"""
from __future__ import annotations

import re

from ... import config
from ..tools.registry import ALLOWED_TOOLS, FORBIDDEN_TOOLS

KEY_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
NODE_TYPES = {"start", "llm", "rag", "tool", "gate", "approval", "output", "artifact"}


def _err(errors: dict[str, list[str]], node_id: str, message: str) -> None:
    errors.setdefault(node_id or "_graph", []).append(message)


def validate_flow(nodes: list, edges: list) -> dict[str, list[str]]:
    """校验工作流图。

    返回 {node_id: [错误, ...]}，空字典 = 通过。
    """
    errors: dict[str, list[str]] = {}

    if not isinstance(nodes, list) or not nodes:
        _err(errors, "_graph", "nodes 必须是非空数组")
        return errors
    if not isinstance(edges, list):
        _err(errors, "_graph", "edges 必须是数组")
        return errors
    if len(nodes) > config.WORKFLOW_MAX_NODES:
        _err(errors, "_graph", f"节点数 {len(nodes)} 超过上限 {config.WORKFLOW_MAX_NODES}")
    if len(edges) > config.WORKFLOW_MAX_EDGES:
        _err(errors, "_graph", f"连线数 {len(edges)} 超过上限 {config.WORKFLOW_MAX_EDGES}")

    ids: list[str] = []
    starts = 0
    for node in nodes:
        if not isinstance(node, dict):
            _err(errors, "_graph", "nodes 里每一项必须是对象")
            continue
        node_id = str(node.get("id") or "")
        if not KEY_RE.match(node_id):
            _err(errors, node_id or "_graph", f"节点 id「{node_id}」非法：只允许字母数字下划线横线，1–32 位")
        if node_id in ids:
            _err(errors, node_id, "节点 id 重复")
        ids.append(node_id)
        node_type = str(node.get("type") or "")
        if node_type not in NODE_TYPES:
            _err(errors, node_id, f"未知节点类型「{node_type}」，可选：{', '.join(sorted(NODE_TYPES))}")
        if node_type == "start":
            starts += 1
        data = node.get("data") or {}
        if not isinstance(data, dict):
            _err(errors, node_id, "data 必须是对象")
            data = {}
        if node_type == "llm":
            prompt = str(data.get("prompt") or "").strip()
            if not prompt:
                _err(errors, node_id, "llm 节点必须有 prompt")
            elif len(prompt) > 8000:
                _err(errors, node_id, "prompt 超过 8000 字上限")
        if node_type == "gate":
            op = str(data.get("op") or "").upper()
            from .gates import GATE_OPS

            if op not in GATE_OPS:
                _err(errors, node_id, f"逻辑门 op「{data.get('op')}」非法，可选：{', '.join(sorted(GATE_OPS))}")
        if node_type == "tool":
            tool = str(data.get("tool") or "").strip()
            if not tool:
                _err(errors, node_id, "tool 节点必须指定工具")
            elif tool in FORBIDDEN_TOOLS:
                _err(errors, node_id, f"工具 {tool} 被平台禁止")
            elif tool not in ALLOWED_TOOLS:
                _err(errors, node_id, f"工具 {tool} 不在平台白名单内")
        if node_type == "approval":
            if not str(data.get("reason") or "").strip():
                _err(errors, node_id, "approval 节点必须写明审批理由")

    if starts != 1:
        _err(errors, "_graph", f"必须恰好有 1 个 start 节点，当前 {starts} 个")

    id_set = set(ids)
    for edge in edges:
        if not isinstance(edge, dict):
            _err(errors, "_graph", "edges 里每一项必须是对象")
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source not in id_set:
            _err(errors, target or source or "_graph", f"连线起点「{source}」不存在")
        if target not in id_set:
            _err(errors, target or source or "_graph", f"连线终点「{target}」不存在")

    # 每个非 start 节点都要有入边，否则永远跑不到
    targets = {str(e.get("target") or "") for e in edges if isinstance(e, dict)}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        node_type = str(node.get("type") or "")
        if node_type != "start" and node_id not in targets:
            _err(errors, node_id, "节点没有入边，永远不会被执行")

    # 环检测（Kahn）
    cycle = _find_cycle(ids, edges)
    if cycle:
        _err(errors, cycle[0], f"工作流存在环：{' → '.join(cycle)}")

    return errors


def _find_cycle(ids: list[str], edges: list) -> list[str]:
    """返回一条环路径，无环则空列表。"""
    graph: dict[str, list[str]] = {i: [] for i in ids}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in graph and target in graph:
            graph[source].append(target)

    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {i: WHITE for i in ids}
    stack: list[str] = []

    def visit(node: str) -> list[str]:
        color[node] = GREY
        stack.append(node)
        for nxt in graph.get(node, []):
            if color.get(nxt) == GREY:
                index = stack.index(nxt)
                return stack[index:] + [nxt]
            if color.get(nxt) == WHITE:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return []

    for node in ids:
        if color.get(node) == WHITE:
            found = visit(node)
            if found:
                return found
    return []


def topological_waves(nodes: list, edges: list) -> list[list[str]]:
    """Kahn 分层：返回每一波（可并行）的节点 id 列表。

    同一波内没有依赖关系，可以并发执行。
    """
    ids = [str(n.get("id")) for n in nodes if isinstance(n, dict)]
    id_set = set(ids)
    deps: dict[str, set[str]] = {i: set() for i in ids}
    succ: dict[str, list[str]] = {i: [] for i in ids}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in id_set and target in id_set:
            deps[target].add(source)
            succ[source].append(target)

    ready = [i for i in ids if not deps[i]]
    waves: list[list[str]] = []
    done: set[str] = set()
    while ready:
        wave = list(ready)
        waves.append(wave)
        done.update(wave)
        ready = []
        for node in wave:
            for nxt in succ[node]:
                if nxt in done:
                    continue
                if deps[nxt] <= done:
                    ready.append(nxt)
    return waves
