"""M31 线上专项：批次 1（S1-1 越权门禁 / S1-4 resume 陈旧提交）的线上验证。

本地 m30 已证代码对；这个脚本证**线上跑的确实是新代码**——两条都选
「不需要真实模型就能触发」的路径，避免把模型抖动算进结论：

  S1-1  A 用户安装工具包后，B 用户的工作流按名调用同一工具 → 必须被门禁拒
        （修复前：A 装的工具进程级共享，B 直接就能用）
  S1-4  工作流暂停在审批节点 → 改图 → resume → 必须 409
        （修复前：旧 outputs 会灌进新图，界面还显示「恢复成功」）

跑法（线上域名 + 分享码）：
    .venv/Scripts/python.exe tests/m31_online_s1.py [baseUrl] [sharecode]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://da-ai.app.workbuddy.host").rstrip("/")
SHARE = sys.argv[2] if len(sys.argv) > 2 else "2GAE2PHAAJ"
PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'  OK ' if ok else '  !! '} {name}" + (f"  [{detail}]" if detail else ""))


class Web:
    def __init__(self) -> None:
        self.jar = __import__("http.cookiejar", fromlist=["CookieJar"]).CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar),
        )

    def raw(self, method: str, path: str, body=None, with_share=True):
        url = BASE + path
        if with_share:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}sharecode={SHARE}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=300) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def json(self, method: str, path: str, body=None):
        status, raw = self.raw(method, path, body)
        try:
            return status, json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return status, {"raw": raw[:200].decode("utf-8", "replace")}

    def sse(self, path: str, body=None):
        status, raw = self.raw("POST", path, body)
        return status, _parse_sse(raw.decode("utf-8", "replace")), raw.decode("utf-8", "replace")

    def enter(self) -> int:
        status, _ = self.raw("GET", "/?sharecode=" + SHARE)
        return status


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    events = []
    for block in raw.split("\n\n"):
        if not block.strip() or block.strip().startswith(":"):
            continue
        name, payload = "message", {}
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    payload = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    payload = {}
        events.append((name, payload))
    return events


def env(key: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


TOOL_GRAPH = {
    "nodes": [
        {"id": "s1", "type": "start", "position": {"x": 0, "y": 0}, "data": {"label": "开始"}},
        {"id": "t1", "type": "tool", "position": {"x": 200, "y": 0},
         "data": {"label": "算一下", "tool": "calc", "input": {"expression": "(12.5+3)*2/4"}}},
        {"id": "o1", "type": "output", "position": {"x": 400, "y": 0},
         "data": {"label": "输出", "text": "结果={{t1}}"}},
    ],
    "edges": [
        {"id": "e1", "source": "s1", "target": "t1"},
        {"id": "e2", "source": "t1", "target": "o1"},
    ],
}

APPROVAL_GRAPH = {
    "nodes": [
        {"id": "s1", "type": "start", "position": {"x": 0, "y": 0}, "data": {"label": "开始"}},
        {"id": "a1", "type": "approval", "position": {"x": 200, "y": 0},
         "data": {"label": "人工确认", "reason": "发布前确认"}},
        {"id": "o1", "type": "output", "position": {"x": 400, "y": 0},
         "data": {"label": "输出", "text": "已批准"}},
    ],
    "edges": [
        {"id": "e1", "source": "s1", "target": "a1"},
        {"id": "e2", "source": "a1", "target": "o1"},
    ],
}


def mint(admin: Web, note: str) -> str:
    status, body = admin.json("POST", "/api/admin/invites",
                              {"count": 1, "max_uses": 1, "days": 7, "daily_quota": 50, "note": note})
    code = (body.get("codes") or [""])[0]
    check(f"建邀请码（{note}）", bool(code), code or str(body)[:80])
    return code


def join(code: str, nickname: str) -> Web:
    user = Web()
    user.enter()
    status, body = user.json("POST", "/api/join", {"code": code, "nickname": nickname})
    check(f"{nickname} 进入", status == 200, f"{status} {str(body)[:80]}")
    return user


def run_graph(user: Web, name: str, graph: dict) -> tuple[str, list[tuple[str, dict]], str]:
    status, created = user.json("POST", "/api/workflows", {"name": name, "graph": graph})
    wf_id = (created or {}).get("id") or ""
    check(f"保存工作流（{name}）", bool(wf_id), str(created)[:80])
    status, events, raw = user.sse(f"/api/workflows/{wf_id}/run", {"inputs": {}})
    return wf_id, events, raw


def main() -> int:
    print("\n[0] 分享门禁对照")
    anon = Web()
    status, _ = anon.raw("GET", "/api/meta", with_share=False)
    check("不带 sharecode 被拦", status == 403, f"got {status}")

    print("\n[1] 管理员与两个用户")
    admin = Web()
    admin.enter()
    status, _ = admin.json("POST", "/api/admin/login", {"password": env("ADMIN_PASSWORD")})
    check("管理员登录", status == 200, f"got {status}")
    a = join(mint(admin, "m31-A"), "m31甲")
    b = join(mint(admin, "m31-B"), "m31乙")

    print("\n[2] S1-1 越权门禁（市场工具按用户隔离）")
    status, body = a.json("POST", "/api/market/install", {"id": "tool.calc"})
    check("A 安装 tool.calc", status == 200, f"{status} {str(body)[:100]}")
    status, tools = a.json("GET", "/api/tools")
    a_calc = next((t for t in (tools.get("tools") or []) if t.get("name") == "calc"), None)
    check("A 的目录里 calc 已启用", bool(a_calc and a_calc.get("enabled")),
          json.dumps(a_calc, ensure_ascii=False)[:120])
    status, tools_b = b.json("GET", "/api/tools")
    b_calc = next((t for t in (tools_b.get("tools") or []) if t.get("name") == "calc"), None)
    # 目录侧口径：市场工具全量可见，但 enabled 按用户门禁（装了才 true）
    check("B 的目录里 calc 可见但未启用（目录侧按用户门禁）",
          bool(b_calc) and not b_calc.get("enabled"),
          json.dumps(b_calc, ensure_ascii=False)[:120])

    _, events_a, raw_a = run_graph(a, "m31-A工具流", TOOL_GRAPH)
    node_events_a = [p for n, p in events_a if n == "node"]
    done_a = [p for p in node_events_a if p.get("status") == "done" and p.get("node_id") == "t1"]
    out_a = json.dumps(done_a[0].get("output") or {}) if done_a else ""
    check("A 跑工具节点成功（装了就能用）",
          bool(done_a) and "7.75" in out_a, out_a[:120] or raw_a[:120])

    _, events_b, raw_b = run_graph(b, "m31-B工具流", TOOL_GRAPH)
    node_events_b = [p for n, p in events_b if n == "node"]
    failed_b = [p for p in node_events_b if p.get("node_id") == "t1" and p.get("status") != "done"]
    err_b = json.dumps(failed_b[0], ensure_ascii=False) if failed_b else ""
    check("B 调同一工具被门禁拒（修复前这里会静默执行）",
          bool(failed_b) and "未安装或已停用" in err_b, err_b[:160] or raw_b[:160])

    print("\n[3] S1-4 resume 防陈旧提交")
    wf_id, events_c, _ = run_graph(a, "m31-审批流", APPROVAL_GRAPH)
    approval = next((p for n, p in events_c if n == "approval"), None)
    run_id = (approval or {}).get("run_id") or ""
    check("审批流暂停并拿到 run_id", bool(run_id), str(approval)[:100])

    if run_id:
        modified = json.loads(json.dumps(APPROVAL_GRAPH))
        modified["nodes"][2]["data"]["text"] = "改过的输出"
        status, upd = a.json("PUT", f"/api/workflows/{wf_id}", {"name": "m31-审批流", "graph": modified})
        check("暂停期间改图（PUT 新版本）", status == 200, f"{status} {str(upd)[:80]}")
        status, body = a.json("POST", f"/api/workflows/runs/{run_id}/resume", {"inputs": {}})
        check("改图后 resume 被 409 拒", status == 409, f"got {status}")
        check("409 文案指向重新运行",
              "已在暂停期间被修改" in json.dumps(body, ensure_ascii=False),
              json.dumps(body, ensure_ascii=False)[:120])

    print("\n[4] 收尾：A 卸载工具包（验证事务化卸载在线也正常）")
    status, body = a.json("POST", "/api/market/uninstall", {"id": "tool.calc"})
    check("A 卸载 tool.calc", status == 200, f"{status} {str(body)[:100]}")
    status, tools = a.json("GET", "/api/tools")
    a_names = [t.get("name") for t in (tools.get("tools") or [])]
    check("卸载后 calc 消失", "calc" not in a_names, str(a_names))

    print(f"\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        for name in FAIL:
            print("  -", name)
        return 1
    print("M31 线上专项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
