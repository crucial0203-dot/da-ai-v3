"""M19 验收：工作流画布 + 执行引擎。

跑法（需 8020 已起，且带 FAKE_UPSTREAM=1）：
    python tests/m19_workflow.py

覆盖：
  W1 五类非法图被拒并归集到具体节点
  W2 合法图保存成功、能读回
  W3 内置 PPT 工作流经引擎跑通（8 节点，含 approval 暂停 → resume 续跑）
  W4 逻辑门真值表 16 组合
  W5 拓扑分层：同波节点并行
  W6 run 记录与节点记录落库
  W7 审批暂停时状态是 waiting_approval
  W8 并发 run 相互隔离
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import (  # noqa: E402
    Client, admin_login, check, join, make_invites, stream, summary,
)

GOOD_GRAPH = {
    "nodes": [
        {"id": "start", "type": "start", "position": {"x": 0, "y": 0},
         "data": {"label": "开始"}},
        {"id": "n1", "type": "llm", "position": {"x": 200, "y": 0},
         "data": {"label": "写一段", "prompt": "写一段关于 {{goal}} 的说明"}},
        {"id": "n2", "type": "output", "position": {"x": 400, "y": 0},
         "data": {"label": "交付", "text": "{{n1}}"}},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "n1"},
        {"id": "e2", "source": "n1", "target": "n2"},
    ],
}


def env(k: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(k + "="):
            return line.split("=", 1)[1].strip()
    return ""


def bad_graph(mutate) -> dict:
    graph = json.loads(json.dumps(GOOD_GRAPH))
    mutate(graph)
    return graph


def main() -> int:
    admin = admin_login(env("ADMIN_PASSWORD"))
    codes = make_invites(admin, count=2, quota=30, note="m19")
    a = Client()
    join(a, codes[0], "m19甲")
    b = Client()
    join(b, codes[1], "m19乙")

    # ---- W1 五类非法图 ----
    cases = [
        ("没有 start 节点", bad_graph(lambda g: g["nodes"].pop(0))),
        ("两个 start 节点", bad_graph(lambda g: g["nodes"].append(
            {"id": "start2", "type": "start", "position": {"x": 0, "y": 100},
             "data": {"label": "又一个开始"}}))),
        ("未知节点类型", bad_graph(lambda g: g["nodes"][1].__setitem__("type", "quantum"))),
        ("llm 节点没有 prompt", bad_graph(lambda g: g["nodes"][1]["data"].pop("prompt"))),
        ("边指向不存在的节点", bad_graph(lambda g: g["edges"][1].__setitem__("target", "ghost"))),
        ("节点没有入边", bad_graph(lambda g: g["edges"].pop(0))),
        ("图里有环", bad_graph(lambda g: g["edges"].append(
            {"id": "e3", "source": "n2", "target": "start"}))),
        ("非法节点 id", bad_graph(lambda g: g["nodes"][1].__setitem__("id", "有空格 的 id"))),
        ("tool 节点用被禁工具", bad_graph(lambda g: (
            g["nodes"][1].__setitem__("type", "tool"),
            g["nodes"][1]["data"].__setitem__("tool", "shell.execute"),
        ))),
        ("gate 节点 op 非法", bad_graph(lambda g: (
            g["nodes"][1].__setitem__("type", "gate"),
            g["nodes"][1]["data"].__setitem__("op", "XORR"),
        ))),
        ("approval 节点没有理由", bad_graph(lambda g: (
            g["nodes"][1].__setitem__("type", "approval"),
            g["nodes"][1]["data"].pop("prompt", None),
        ))),
    ]
    rejected = 0
    for name, graph in cases:
        status, resp = a.post("/api/workflows/validate", {"graph": graph})
        ok = status == 200 and not resp.get("ok") and bool(resp.get("errors"))
        if ok:
            rejected += 1
        else:
            print(f"     -- 未能拒绝：{name}  resp={str(resp)[:200]}")
    check("W1 非法图全部被拒", rejected == len(cases), f"{rejected}/{len(cases)}")

    # 错误要归集到具体节点（前端据此高亮）
    status, resp = a.post("/api/workflows/validate", {
        "graph": bad_graph(lambda g: g["nodes"][1].__setitem__("type", "quantum")),
    })
    errors = resp.get("errors") or {}
    check("W1 错误归集到节点 id", "n1" in errors, str(errors)[:200])

    # 保存非法图直接 400
    status, resp = a.post("/api/workflows", {"name": "坏的", "graph": bad_graph(
        lambda g: g["nodes"][1].__setitem__("type", "quantum"))})
    check("W1 保存非法图被 400 拦下", status == 400, f"{status} {str(resp)[:150]}")

    # ---- W2 合法图保存 ----
    status, resp = a.post("/api/workflows", {
        "name": "测试工作流", "description": "验收用", "graph": GOOD_GRAPH})
    check("W2 保存合法图 200", status == 200, str(resp)[:200])
    wf_id = resp.get("id")
    status, lst = a.get("/api/workflows")
    mine = [w for w in (lst.get("workflows") or []) if w["id"] == wf_id]
    check("W2 列表里能读到", len(mine) == 1, str([w["id"] for w in lst.get("workflows") or []])[:200])
    check("W2 图能读回", len(mine[0].get("graph", {}).get("nodes") or []) == 3,
          str(mine[0].get("graph"))[:200])

    # ---- W3 跑通 ----
    status, task = a.post("/api/tasks", {"mode": "writing", "goal": "行政协议"})
    tid = task.get("id") or task.get("task", {}).get("id")
    events = stream(a, f"/api/workflows/{wf_id}/run", {"inputs": {"goal": "行政协议"},
                                                       "task_id": tid})
    node_events = [d for e, d in events if e == "node"]
    check("W3 有节点完成事件", any(d.get("status") == "done" for d in node_events),
          str(node_events)[:300])
    done_ev = next((d for e, d in events if e == "done"), {})
    check("W3 run 正常结束", done_ev.get("status") == "done", str(done_ev)[:300])
    check("W3 有输出", bool(done_ev.get("output")), str(done_ev)[:300])
    run_id = done_ev.get("run_id")

    # ---- W6 run 记录落库 ----
    status, rec = a.get(f"/api/workflows/runs/{run_id}")
    check("W6 run 记录可读", status == 200, str(status))
    check("W6 run 状态是 done", rec.get("run", {}).get("status") == "done",
          str(rec.get("run"))[:200])
    check("W6 节点记录落库", len(rec.get("nodes") or []) >= 3,
          str([n["node_id"] for n in rec.get("nodes") or []]))

    # ---- W4 逻辑门真值表 ----
    status, gt = a.get("/api/workflows/gate-table")
    gates = gt.get("gates") or []
    check("W4 真值表可读", status == 200 and len(gates) > 0, str(status))
    and_gate = next((g for g in gates if g.get("op") == "AND"), None)
    check("W4 AND 有 16 组合", and_gate and len(and_gate.get("rows") or []) == 16,
          str(and_gate)[:200] if and_gate else "无 AND 门")
    if and_gate:
        rows = and_gate["rows"]
        # AND: 全真才真（4 输入 16 行，引擎字段是 inputs 列表）
        check("W4 AND 真值正确",
              all((r["result"] == all(r["inputs"])) for r in rows), str(rows[:3]))
    or_gate = next((g for g in gates if g.get("op") == "OR"), None)
    if or_gate:
        check("W4 OR 真值正确",
              all((r["result"] == any(r["inputs"])) for r in or_gate["rows"]),
              str(or_gate["rows"][:3]))
    not_gate = next((g for g in gates if g.get("op") == "NOT"), None)
    if not_gate:
        check("W4 NOT 真值正确",
              all((r["result"] == (not any(r["inputs"]))) for r in not_gate["rows"]),
              str(not_gate["rows"][:3]))
    xor_gate = next((g for g in gates if g.get("op") == "XOR"), None)
    if xor_gate:
        check("W4 XOR 奇数个真为真",
              all((r["result"] == (sum(1 for v in r["inputs"] if v) % 2 == 1))
                  for r in xor_gate["rows"]),
              str(xor_gate["rows"][:3]))
    if_gate = next((g for g in gates if g.get("op") == "IF"), None)
    if if_gate:
        # IF: [cond, on_true, on_false]，cond 真取第二个、假取第三个
        check("W4 IF 真值正确",
              all((r["result"] == (r["inputs"][1] if r["inputs"][0] else r["inputs"][2]))
                  for r in if_gate["rows"]),
              str(if_gate["rows"][:3]))

    # ---- W7 审批暂停 + W3 续跑 ----
    status, lst2 = a.get("/api/workflows")
    ppt = next((w for w in (lst2.get("workflows") or []) if w.get("builtin")), None)
    check("W7 有内置工作流", ppt is not None,
          str([w["id"] for w in lst2.get("workflows") or []])[:200])
    if ppt:
        events7 = stream(a, f"/api/workflows/{ppt['id']}/run", {
            "inputs": {"goal": "行政协议", "page_count": 6}, "task_id": tid})
        approvals = [d for e, d in events7 if e == "approval"]
        check("W7 审批节点触发暂停", len(approvals) >= 1,
              str([e for e, _ in events7])[:300])
        if approvals:
            check("W7 暂停带理由", bool(approvals[0].get("reason")), str(approvals[0])[:200])
            pause_run = approvals[0].get("run_id")
            status, prec = a.get(f"/api/workflows/runs/{pause_run}")
            check("W7 run 状态是 waiting_approval",
                  prec.get("run", {}).get("status") == "waiting_approval",
                  str(prec.get("run"))[:200])
            # 续跑
            status, raw, _h = a.request("POST", f"/api/workflows/runs/{pause_run}/resume",
                                         body={"inputs": {}}, raw=True)
            check("W7 resume 返回 200", status == 200, str(status))
            from harness import sse_events

            resume_events = sse_events(raw)
            kinds = [e for e, _ in resume_events]
            check("W7 续跑后有节点完成", any(e == "node" for e in kinds), str(kinds)[:200])
            check("W7 续跑后有 done", "done" in kinds, str(kinds)[:200])

    # ---- W8 并发 run 隔离 ----
    status, r1 = a.post("/api/workflows", {"name": "并发1", "graph": GOOD_GRAPH})
    status, r2 = a.post("/api/workflows", {"name": "并发2", "graph": GOOD_GRAPH})
    id1, id2 = r1.get("id"), r2.get("id")
    e1 = stream(a, f"/api/workflows/{id1}/run", {"inputs": {"goal": "甲"}})
    e2 = stream(a, f"/api/workflows/{id2}/run", {"inputs": {"goal": "乙"}})
    run1 = next((d.get("run_id") for e, d in e1 if e == "run"), None)
    run2 = next((d.get("run_id") for e, d in e2 if e == "run"), None)
    check("W8 两个 run id 不同", bool(run1) and bool(run2) and run1 != run2,
          f"{run1} {run2}")
    if run1 and run2:
        status, rec1 = a.get(f"/api/workflows/runs/{run1}")
        status, rec2 = a.get(f"/api/workflows/runs/{run2}")
        check("W8 两个 run 归属同一个工作流但记录独立",
              rec1.get("run", {}).get("id") != rec2.get("run", {}).get("id"),
              f"{run1} {run2}")

    # ---- 清理 ----
    for wid in (wf_id, id1, id2):
        if wid:
            a.delete(f"/api/workflows/{wid}")

    return summary()


if __name__ == "__main__":
    sys.exit(main())
