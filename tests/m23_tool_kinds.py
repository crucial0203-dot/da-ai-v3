"""M23 验收：工具语义分类（kind）——后端映射表 + SSE tool 事件新字段。

跑法（需 8020 已起，且带 FAKE_UPSTREAM=1）：
    python tests/m23_tool_kinds.py

覆盖：
  单元层  26 个已知工具名全部映射到合法 kind（含 12 个市场工具包）
          label 走注册表 / 兜底；未知名字落 other；tool_meta 契约
  SSE 层  tool 事件带 call_id/kind/label/ts/seq
          call_id 复用语义：一次调用的 running/done 同 id，两轮同工具不同 id
          白名单拒绝与审批 waiting 也带全字段
          事件里的 kind 与后端映射表逐条比对（防前后端漂移）
          市场工具包端到端：装 date_calc → 调用 → kind=execute → 卸
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import (  # noqa: E402
    Client, admin_login, check, join, make_invites, stream, summary,
)

from app.services.tools import kinds  # noqa: E402

# 全部已知工具名：7 个 V3 技能工具 + 7 个 V4 具名工具 + 12 个市场工具包
KNOWN_TOOLS = [
    "file.read", "reference.verify", "minimax.generate",
    "pptx.create", "pptx.modify", "pptx.validate", "artifact.write",
    "web_search", "fetch_url", "rag_query", "memory_query",
    "artifact_create", "doc_generate", "file_list",
    "calc", "text_stats", "render_template", "date_calc", "unit_convert",
    "json_format", "regex_test", "md_table", "text_dedupe",
    "cn_number", "rand_gen", "encode_text",
]


def unit_kinds() -> None:
    for name in KNOWN_TOOLS:
        kind = kinds.kind_of(name)
        check(f"映射 {name} → {kind}", kind in kinds.KINDS, kind)
    check("合成事件 _parse 有映射", kinds.kind_of("_parse") == "other")
    check("未知工具落 other", kinds.kind_of("不存在的工具") == "other")
    check("KINDS 恰好十类", len(kinds.KINDS) == 10, str(len(kinds.KINDS)))
    used = {kinds.kind_of(n) for n in KNOWN_TOOLS}
    check("delete 类有工具", "delete" in used, str(used))
    for need in ("read", "edit", "search", "execute", "think", "fetch"):
        check(f"{need} 类有工具", need in used, str(used))

    check("label 取注册表（web_search）", kinds.label_of("web_search") == "联网检索",
          kinds.label_of("web_search"))
    check("label 取注册表（doc_generate）", kinds.label_of("doc_generate") == "生成文档",
          kinds.label_of("doc_generate"))
    # 工具包未安装时进程级注册表里没有它，label 按契约退化为名字本身。
    # SSE 层只有装了的包才可能被调用——真实链路的工具包 label 由 S6 验。
    check("label 未安装工具包退化为名字", kinds.label_of("calc") == "calc",
          kinds.label_of("calc"))
    check("label _parse 兜底", kinds.label_of("_parse") == "工具调用解析")
    check("label 未知工具退化为名字", kinds.label_of("神秘工具") == "神秘工具")

    meta = kinds.tool_meta("web_search")
    check("tool_meta 契约", meta.get("kind") == "search" and meta.get("label") == "联网检索",
          str(meta))
    for kind in kinds.KINDS:
        check(f"KIND_LABELS 覆盖 {kind}", bool(kinds.KIND_LABELS.get(kind)))


def tool_events(events) -> list[dict]:
    return [d for e, d in events if e == "tool"]


def main() -> int:
    unit_kinds()

    import os
    password = ""
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("ADMIN_PASSWORD="):
            password = line.split("=", 1)[1].strip()
    admin = admin_login(password)
    codes = make_invites(admin, count=3, quota=40, note="m23")
    visitors = []
    for i, code in enumerate(codes):
        c = Client()
        join(c, code, f"m23分类{i}")
        visitors.append(c)

    def rotate(i: int) -> Client:
        """chat 限流 6 次/分钟/访客：三个访客轮流发。"""
        return visitors[i % len(visitors)]

    # ---- S1 单轮：running/done 同 call_id，字段齐全 ----
    c0 = rotate(0)
    _, task = c0.post("/api/tasks", {"mode": "research", "goal": "kind 单轮"})
    tid = task.get("id") or task.get("task", {}).get("id")
    events = stream(c0, f"/api/tasks/{tid}/chat", {
        "text": '#fake #tool {"tool_name":"web_search","tool_input":{"query":"行政协议"}}',
    })
    te = tool_events(events)
    runs = [t for t in te if t.get("status") == "running"]
    dones = [t for t in te if t.get("status") == "done"]
    check("S1 有 running 与 done", bool(runs) and bool(dones), str(te)[:300])
    if runs and dones:
        check("S1 running/done 同 call_id", runs[0]["call_id"] == dones[0]["call_id"],
              f"{runs[0].get('call_id')} vs {dones[0].get('call_id')}")
        check("S1 call_id 非空", bool(runs[0].get("call_id")), str(runs[0].get("call_id")))
    for field in ("kind", "label", "ts", "seq"):
        check(f"S1 running 带 {field}", field in runs[0] if runs else False,
              str(runs[0])[:200] if runs else "no running")
    if runs:
        check("S1 kind 正确（search）", runs[0].get("kind") == "search", str(runs[0].get("kind")))
        check("S1 label 正确", runs[0].get("label") == "联网检索", str(runs[0].get("label")))
        check("S1 running 带参数", bool(runs[0].get("input")), str(runs[0])[:200])
        check("S1 ts 是数字", isinstance(runs[0].get("ts"), (int, float)), str(runs[0].get("ts")))
        check("S1 seq 是数字", isinstance(runs[0].get("seq"), int), str(runs[0].get("seq")))

    # ---- S2 两轮同工具：call_id 必须不同，seq 单调 ----
    c1 = rotate(1)
    _, task2 = c1.post("/api/tasks", {"mode": "research", "goal": "kind 两轮"})
    tid2 = task2.get("id") or task2.get("task", {}).get("id")
    events2 = stream(c1, f"/api/tasks/{tid2}/chat", {
        "text": '#fake #tool {"tool_name":"web_search","tool_rounds":2}',
    })
    te2 = tool_events(events2)
    runs2 = [t for t in te2 if t.get("status") == "running"]
    check("S2 两轮都跑了", len(runs2) >= 2, f"runs={len(runs2)}")
    if len(runs2) >= 2:
        check("S2 两轮 call_id 不同", runs2[0]["call_id"] != runs2[1]["call_id"],
              f"{runs2[0].get('call_id')} vs {runs2[1].get('call_id')}")
    seqs = [t.get("seq") for t in te2 if isinstance(t.get("seq"), int)]
    check("S2 seq 单调递增", seqs == sorted(seqs) and len(set(seqs)) == len(seqs), str(seqs))
    dones2 = [t for t in te2 if t.get("status") == "done"]
    run_ids = {t.get("call_id") for t in runs2}
    check("S2 每个 running 都有配对的 done",
          all(d.get("call_id") in run_ids for d in dones2),
          f"runs={sorted(run_ids)} dones={[d.get('call_id') for d in dones2]}")

    # ---- S3 白名单拒绝：error 事件也带全字段 ----
    c2 = rotate(2)
    _, task3 = c2.post("/api/tasks", {"mode": "research", "goal": "kind 越权"})
    tid3 = task3.get("id") or task3.get("task", {}).get("id")
    events3 = stream(c2, f"/api/tasks/{tid3}/chat", {
        "text": '#fake #tool {"tool_name":"shell.execute","tool_input":{"cmd":"ls"}}',
    })
    te3 = tool_events(events3)
    errs3 = [t for t in te3 if t.get("status") == "error"]
    check("S3 越权被拒", bool(errs3), str(te3)[:300])
    if errs3:
        check("S3 拒绝也带 call_id/kind/label",
              bool(errs3[0].get("call_id")) and errs3[0].get("kind") == "other"
              and errs3[0].get("label") == "shell.execute",
              str(errs3[0])[:200])
        check("S3 拒绝事件带 attempted input（复现现场）",
              errs3[0].get("input") == {"cmd": "ls"}, str(errs3[0].get("input")))

    # ---- S4 审批 waiting：kind=edit，call_id 与 running 一致 ----
    c0 = rotate(3)
    _, task4 = c0.post("/api/tasks", {"mode": "slides", "goal": "kind 审批"})
    tid4 = task4.get("id") or task4.get("task", {}).get("id")
    events4 = stream(c0, f"/api/tasks/{tid4}/chat", {
        "text": '#fake #tool {"tool_name":"doc_generate","tool_input":{"doc_type":"docx","goal":"纪要"}}',
    })
    te4 = tool_events(events4)
    wait4 = [t for t in te4 if t.get("status") == "waiting"]
    run4 = [t for t in te4 if t.get("status") == "running"]
    check("S4 进入 waiting", bool(wait4), str(te4)[:300])
    approvals4 = [d for e, d in events4 if e == "approval"]
    check("S4 有 approval 事件", bool(approvals4), str(approvals4)[:300])
    if wait4:
        check("S4 waiting kind=edit", wait4[0].get("kind") == "edit", str(wait4[0].get("kind")))
        check("S4 waiting label=生成文档", wait4[0].get("label") == "生成文档",
              str(wait4[0].get("label")))
        if run4:
            check("S4 waiting/running 同 call_id",
                  wait4[0].get("call_id") == run4[0].get("call_id"),
                  f"{wait4[0].get('call_id')} vs {run4[0].get('call_id')}")
    if approvals4:
        ap = approvals4[0]
        check("S4 approval 带 label/kind", ap.get("label") == "生成文档" and ap.get("kind") == "edit",
              str({k: ap.get(k) for k in ("label", "kind")}))
        check("S4 写操作审批 risk=high", ap.get("risk") == "high", str(ap.get("risk")))
        details = ap.get("details") or []
        labels = [d.get("label") for d in details]
        check("S4 details 逐字段（doc_type/goal）",
              "doc_type" in labels and "goal" in labels, str(labels))
        check("S4 details value 来自 input",
              any(d.get("value") == "docx" for d in details), str(details)[:200])

    # ---- S5 防漂移：事件里的 kind 必须等于后端映射表 ----
    drift = []
    for ev in (te + te2 + te3 + te4):
        name = ev.get("name")
        if name and kinds.kind_of(str(name)) != ev.get("kind"):
            drift.append((name, ev.get("kind"), kinds.kind_of(str(name))))
    check("S5 事件 kind 与后端映射一致（防漂移）", not drift, str(drift)[:300])

    # ---- S6 市场工具包端到端：装 date_calc → 调用 → kind=execute ----
    c1 = rotate(4)
    status, r = c1.post("/api/market/install", {"id": "tool.date-calc"})
    check("S6 安装 tool.date-calc", status == 200, str(r)[:120])
    _, task6 = c1.post("/api/tasks", {"mode": "research", "goal": "kind 工具包"})
    tid6 = task6.get("id") or task6.get("task", {}).get("id")
    events6 = stream(c1, f"/api/tasks/{tid6}/chat", {
        "text": '#fake #tool {"tool_name":"date_calc","tool_input":{"start":"2026-10-01","days":30}}',
    })
    te6 = tool_events(events6)
    pack = [t for t in te6 if t.get("name") == "date_calc"]
    check("S6 date_calc 被调用", bool(pack), str(te6)[:300])
    if pack:
        check("S6 工具包 kind=execute", pack[0].get("kind") == "execute", str(pack[0].get("kind")))
        check("S6 工具包 label 来自注册表", pack[0].get("label") == "日期计算",
              str(pack[0].get("label")))
        check("S6 工具包 call_id 配对",
              len({t.get("call_id") for t in pack}) == 1 and bool(pack[0].get("call_id")),
              str([t.get("call_id") for t in pack]))
    c1.post("/api/market/uninstall", {"id": "tool.date-calc"})

    # ---- S7 错误三级：上游 502 → error + retryable ----
    c2 = rotate(5)
    _, task7 = c2.post("/api/tasks", {"mode": "research", "goal": "kind 错误分级"})
    tid7 = task7.get("id") or task7.get("task", {}).get("id")
    events7 = stream(c2, f"/api/tasks/{tid7}/chat", {"text": '#fake {"fail":"mid"}'})
    errs7 = [d for e, d in events7 if e == "error"]
    check("S7 上游 502 发 error 事件", bool(errs7), str(errs7)[:200])
    if errs7:
        check("S7 502 → severity=error", errs7[0].get("severity") == "error",
              str(errs7[0].get("severity")))
        check("S7 502 → retryable=true", errs7[0].get("retryable") is True,
              str(errs7[0].get("retryable")))

    return summary()


if __name__ == "__main__":
    sys.exit(main())
