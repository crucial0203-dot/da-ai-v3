"""M18 验收：Artifacts 制品预览。

跑法（需 8020 已起）：
    python tests/m18_artifacts.py

覆盖：
  A1 三块拼合成完整 HTML 文档（html/css/js 都在）
  A2 render 路由下发独立 CSP（default-src 'none' + connect-src 'none'）
  A3 render 页里有 meta CSP 双保险
  A4 跨用户访问被拒
  A5 版本：建新版本 → 恢复旧版本 → 版本号递增、历史不被覆盖
  A6 工具 artifact_create 能出制品，且 done 事件带 artifacts
  A7 危险内容被清洗（javascript: 协议、外部 iframe）
  A8 列表按用户隔离
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import (  # noqa: E402
    Client, admin_login, check, join, make_invites, stream, summary,
)

SAMPLE_HTML = "<h1>行政协议三要素</h1><p>主体、目的、内容。</p>"
SAMPLE_CSS = "h1 { color: #1a4d8f; font-size: 22px; }"
SAMPLE_JS = "document.querySelector('h1').dataset.ready = '1';"


def env(k: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(k + "="):
            return line.split("=", 1)[1].strip()
    return ""


def main() -> int:
    admin = admin_login(env("ADMIN_PASSWORD"))
    codes = make_invites(admin, count=2, quota=30, note="m18")
    a = Client()
    join(a, codes[0], "m18甲")
    b = Client()
    join(b, codes[1], "m18乙")

    # ---- A1 三块拼合 ----
    status, resp = a.post("/api/artifacts", {
        "title": "三要素图",
        "html": SAMPLE_HTML,
        "css": SAMPLE_CSS,
        "js": SAMPLE_JS,
    })
    check("A1 创建制品 200", status == 200, str(resp)[:200])
    art_id = resp.get("id")
    check("A1 返回制品 id", bool(art_id), str(resp)[:200])

    status, detail = a.get(f"/api/artifacts/{art_id}")
    check("A1 制品详情可读", status == 200, str(status))
    html = detail.get("html") or ""
    check("A1 是完整 HTML 文档", html.lstrip().lower().startswith("<!doctype html"), html[:80])
    check("A1 html 正文在里面", "行政协议三要素" in html, html[:200])
    check("A1 css 在里面", "font-size: 22px" in html, html[:300])
    check("A1 js 在里面", "dataset.ready" in html, html[:400])
    check("A1 style 标签包住 css", "<style>" in html and "</style>" in html, html[:300])

    # ---- A2/A3 render 的 CSP ----
    status, raw, headers = a.request("GET", f"/api/artifacts/{art_id}/render", raw=True)
    check("A2 render 200", status == 200, str(status))
    csp = headers.get("Content-Security-Policy") or headers.get("content-security-policy") or ""
    check("A2 有 CSP 响应头", bool(csp), str(list(headers)))
    check("A2 CSP 是 default-src 'none'", "default-src 'none'" in csp, csp)
    check("A2 CSP 禁 connect", "connect-src 'none'" in csp, csp)
    check("A2 CSP 禁 frame-ancestors 外链", "frame-ancestors 'self'" in csp, csp)
    check("A2 CSP 允许 inline script", "script-src 'unsafe-inline'" in csp, csp)
    check("A2 有 nosniff", (headers.get("X-Content-Type-Options")
                        or headers.get("x-content-type-options") or "") == "nosniff",
          str(headers))
    check("A3 页面里有 meta CSP 双保险",
          "http-equiv=\"Content-Security-Policy\"" in raw
          or "http-equiv='Content-Security-Policy'" in raw,
          raw[:300])
    check("A3 meta CSP 内容与响应头一致",
          "default-src" in raw and "connect-src" in raw, raw[:400])

    # ---- A4 跨用户 ----
    status, _ = b.get(f"/api/artifacts/{art_id}")
    check("A4 别人看不到这个制品", status == 404, str(status))
    status, _raw, _h = b.request("GET", f"/api/artifacts/{art_id}/render", raw=True)
    check("A4 别人也渲染不了", status == 404, str(status))

    # ---- A5 版本 ----
    status, resp5 = a.post(f"/api/artifacts/{art_id}/versions", {
        "title": "三要素图", "html": "<h1>第二版</h1>", "css": "", "js": "",
    })
    check("A5 建新版本 200", status == 200, str(resp5)[:200])
    v2 = resp5.get("id")
    status, d5 = a.get(f"/api/artifacts/{v2}")
    check("A5 新版本内容不同", "第二版" in (d5.get("html") or ""), str(d5.get("html"))[:200])
    check("A5 新版本号是 2", d5.get("version") == 2, str(d5.get("version")))

    status, resp5b = a.post(f"/api/artifacts/{art_id}/restore", {})
    check("A5 恢复旧版本 200", status == 200, str(resp5b)[:200])
    v3 = resp5b.get("id")
    status, d5b = a.get(f"/api/artifacts/{v3}")
    check("A5 恢复后版本号递增", d5b.get("version") == 3, str(d5b.get("version")))
    check("A5 恢复后内容回到第一版",
          "行政协议三要素" in (d5b.get("html") or ""), str(d5b.get("html"))[:200])
    status, d1 = a.get(f"/api/artifacts/{art_id}")
    check("A5 历史版本没被覆盖",
          "行政协议三要素" in (d1.get("html") or ""), str(d1.get("html"))[:120])

    # ---- A7 危险内容清洗 ----
    status, resp7 = a.post("/api/artifacts", {
        "title": "危险内容",
        "html": ('<a href="javascript:alert(1)">点我</a>'
                 '<iframe src="http://evil.example/"></iframe>'
                 '<script src="http://evil.example/x.js"></script>'
                 '<p>正常内容</p>'),
    })
    art7 = resp7.get("id")
    check("A7 危险制品建得出来", status == 200 and bool(art7), str(resp7)[:200])
    if art7:
        status, d7 = a.get(f"/api/artifacts/{art7}")
        html7 = d7.get("html") or ""
        check("A7 javascript: 协议被拦", "javascript:" not in html7.lower(), html7[:300])
        check("A7 外链 iframe 被去掉", "<iframe" not in html7.lower(), html7[:300])
        check("A7 外部 script 被去掉",
              "evil.example" not in html7.lower(), html7[:300])
        check("A7 正常内容保留", "正常内容" in html7, html7[:300])

    # ---- A8 列表隔离 ----
    status, la = a.get("/api/artifacts")
    status, lb = b.get("/api/artifacts")
    ids_a = {x["id"] for x in (la.get("artifacts") or [])}
    ids_b = {x["id"] for x in (lb.get("artifacts") or [])}
    check("A8 A 的列表里有自己的制品", art_id in ids_a, str(sorted(ids_a))[:200])
    check("A8 B 的列表里没有 A 的制品", not (ids_a & ids_b), str(sorted(ids_a & ids_b))[:200])

    # ---- A6 工具出制品 ----
    status, task6 = a.post("/api/tasks", {"mode": "writing", "goal": "做个图表"})
    tid6 = task6.get("id") or task6.get("task", {}).get("id")
    events6 = stream(a, f"/api/tasks/{tid6}/chat", {
        "text": ('#fake #tool {"tool_name":"artifact_create","tool_input":'
                 '{"title":"测试图","html":"<h1>图</h1>"}}'),
    })
    te6 = [d for e, d in events6 if e == "tool"]
    check("A6 artifact_create 跑成功",
          any(t.get("status") == "done" and t.get("name") == "artifact_create" for t in te6),
          str(te6)[:300])
    done6 = next((d for e, d in events6 if e == "done"), {})
    check("A6 done 带 artifacts 列表", bool(done6.get("artifacts")), str(done6)[:300])
    if done6.get("artifacts"):
        new_art = done6["artifacts"][0]
        status, _ = a.get(f"/api/artifacts/{new_art}")
        check("A6 done 里的制品能取到", status == 200, str(status))

    return summary()


if __name__ == "__main__":
    sys.exit(main())
