"""M7 线上验收：打公网域名，验 9 项。

跑法（用项目虚拟环境，回读 docx 需要 python-docx）：
    .venv/Scripts/python.exe tests/m7_online.py [baseUrl] [sharecode]

线上有两个平台行为必须按它们的规则来，否则测出来的失败全是假的：
1. 分享门禁**每个请求都要带 ?sharecode=**（2026-10-23 实测：平台已不再下发免参
   cookie，enter() 只负责首次进入首页）；
2. /healthz 被平台自己占用（返回 Pod alive），应用自己的健康检查走 /__health。
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docx import Document  # noqa: E402

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://da-ai.app.workbuddy.host").rstrip("/")
SHARE = sys.argv[2] if len(sys.argv) > 2 else "2GAE2PHAAJ"
TMP = ROOT / "data" / "tmp" / "m7"
PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'  OK ' if ok else '  !! '} {name}" + (f"  [{detail}]" if detail else ""))


def read_env() -> dict:
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


class Web:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar),
        )

    def raw(self, method: str, path: str, body=None, headers=None, with_share=True):
        # 门禁现状（2026-10-23 实测）：平台不再下发免参 cookie，每个请求都必须带
        # ?sharecode=，所以除门禁对照组外默认自动附带。
        url = BASE + path
        if with_share:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}sharecode={SHARE}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with self.opener.open(req, timeout=300) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def json(self, method: str, path: str, body=None, headers=None):
        status, raw, _ = self.raw(method, path, body, headers)
        try:
            return status, json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return status, {"raw": raw[:200].decode("utf-8", "replace")}

    def sse(self, path: str, body=None):
        status, raw, _ = self.raw("POST", path, body)
        text = raw.decode("utf-8", "replace")
        return status, parse_sse(text), text

    def enter(self) -> int:
        """过分享门禁：带 sharecode 打开一次首页，拿 cookie。"""
        status, _, _ = self.raw("GET", "/?sharecode=" + SHARE)
        return status


def parse_sse(raw: str) -> list[tuple[str, dict]]:
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


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    anon = Web()

    print("\n[1] 分享门禁")
    # 门禁只拦 API，静态首页放行；对照组用 /api/meta 验「无 code 被拦」
    status, _, _ = anon.raw("GET", "/api/meta", with_share=False)
    check("不带 sharecode 被拦", status == 403, f"got {status}")
    check("带 sharecode 放行", anon.enter() == 200)

    print("\n[2] 首页")
    status, raw, headers = anon.raw("GET", "/")
    html = raw.decode("utf-8", "replace")
    check("首页 200", status == 200, f"got {status}")
    check("是真首页不是门禁页",
          "DA的AI" in html and ("app.js" in html or "ui/bundle" in html),
          html[:60].replace("\n", " "))
    csp = headers.get("Content-Security-Policy") or headers.get("content-security-policy") or ""
    check("带 CSP 响应头", "default-src" in csp, csp[:60])

    print("\n[3] 静态资源（按首页引用解析，兼容旧版多入口与 V4 打包产物）")
    import re
    refs = re.findall(r'(?:href|src)="(/[^"]+\.(?:css|js))"', html)
    for path in refs[:4] or ["/css/app.css"]:
        status, raw, _ = anon.raw("GET", path)
        check(f"{path} 200", status == 200 and len(raw) > 500, f"{status} {len(raw)}B")

    print("\n[4] 应用健康（/healthz 被平台占用，走 /__health）")
    status, raw, _ = anon.raw("GET", "/__health")
    try:
        health = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        health = {}
    check("健康检查 200", status == 200 and health.get("ok") is True, f"{status} {str(health)[:80]}")
    check("数据库就绪", health.get("db") == "ok", str(health.get("db")))
    check("模型 Key 已配置", health.get("key_configured") is True)
    data_dir = str(health.get("data_dir") or "")
    check("数据落在 /opt（跨发布会保留）", data_dir.startswith("/opt/"), data_dir or "未知")
    journal = health.get("journal")
    print(f"      日志模式：{journal}（wal 最好，delete 说明沙箱 WAL 也不可用）")

    print("\n[5] 接口元信息")
    status, meta = anon.json("GET", "/api/meta")
    check("/api/meta 200", status == 200, f"got {status}")
    check("兜底额度 5 次（V3.1 起主额度跟码）", meta.get("daily_calls") == 5, str(meta.get("daily_calls")))
    unverified = [c["key"] for c in meta.get("capabilities") or [] if not c["verified"]]
    verified = [c["key"] for c in meta.get("capabilities") or [] if c["verified"]]
    check("能力矩阵有未验证项且如实标注", len(unverified) >= 3, f"未验证 {unverified}")
    check("已验证能力就是那 5 项",
          sorted(verified) == ["docx.create", "docx.read_format_sample", "pdf.extract_text",
                              "pptx.create", "pptx.fill_template"], str(sorted(verified)))

    print("\n[6] 邀请码进入")
    admin = Web()
    admin.enter()
    env = read_env()
    status, _ = admin.json("POST", "/api/admin/login", {"password": env.get("ADMIN_PASSWORD", "")})
    check("管理员登录 200", status == 200, f"got {status}")
    status, body = admin.json("POST", "/api/admin/invites",
                              {"count": 1, "max_uses": 1, "days": 7, "daily_quota": 50, "note": "线上验收"})
    code = (body.get("codes") or [""])[0]
    check("管理员建码", bool(code), code or str(body)[:80])

    guest = Web()
    guest.enter()
    status, body = guest.json("POST", "/api/join", {"code": code, "nickname": "线上验收"})
    check("凭码进入 200", status == 200, f"{status} {body}")
    status, me = guest.json("GET", "/api/me")
    check("/api/me 是访客且额度跟码 50",
          status == 200 and (me.get("quota") or {}).get("quota") == 50,
          f"{status} quota={(me.get('quota') or {}).get('quota')}")

    print("\n[7] 流式对话（真实模型）")
    status, body = guest.json("POST", "/api/tasks",
                              {"mode": "writing", "goal": "线上验收：说说这条链路通不通"})
    task_id = ((body or {}).get("task") or {}).get("id") or ""
    check("建任务", bool(task_id), task_id or str(body)[:80])
    started = time.time()
    status, events, raw = guest.sse(f"/api/tasks/{task_id}/chat", {"text": "只回六个字：线上链路通了"})
    names = [n for n, _ in events]
    text = "".join(p.get("t", "") for n, p in events if n == "text")
    check("对话流 200", status == 200, f"got {status}")
    check("收到正文", bool(text.strip()), text[:30] or raw[:80])
    check("以 done 收尾", names and names[-1] == "done", str(names[-3:]))
    print(f"      （耗时 {time.time() - started:.1f}s，事件 {names.count('text')} 段）")

    print("\n[8] 生成 Word 并回读（真实模型）")
    started = time.time()
    status, events, raw = guest.sse(f"/api/tasks/{task_id}/generate",
                                    {"word_count": 400, "effort": "low"})
    result = next((p for n, p in events if n == "result"), {})
    check("生成成功", bool(result.get("ok")), json.dumps(result, ensure_ascii=False)[:120])
    print(f"      （耗时 {time.time() - started:.1f}s）")
    file_id = result.get("file_id")
    if file_id:
        status, blob, _ = guest.raw("GET", f"/api/files/{file_id}")
        got = TMP / "online.docx"
        got.write_bytes(blob)
        check("下载回 docx", status == 200 and blob[:2] == b"PK", f"{status} {len(blob)}B")
        doc = Document(str(got))
        paras = [p.text for p in doc.paragraphs if p.text.strip()]
        words = sum(len(t) for t in paras)
        check("正文可读且字数达标", words >= 200, f"{words} 字 / {len(paras)} 段")
        footer = "word/footer1.xml"
        has_page = footer in zipfile.ZipFile(got).namelist()
        check("带页码域", has_page)
        report = result.get("report") or {}
        check("服务端回读断言全过", report.get("ok") is True,
              f"{report.get('passed')}/{report.get('total')}")
    else:
        check("拿到文件 id", False, str(result)[:120])

    print("\n[9] 边界")
    fresh = Web()
    fresh.enter()
    status, _ = fresh.json("GET", "/api/admin/overview")
    check("无会话打管理接口 401", status == 401, f"got {status}")
    status, _ = guest.json("GET", "/api/admin/visitors")
    check("访客打管理接口 403", status == 403, f"got {status}")
    status, body = guest.json("POST", f"/api/tasks/{task_id}/chat", {"text": "跨站测试"},
                              headers={"Origin": "https://evil.example.com"})
    check("伪造 Origin 的写请求被拒 403", status == 403, f"{status} {str(body)[:60]}")
    status, body = guest.json("POST", f"/api/tasks/{task_id}/chat", {"text": "同源测试"},
                              headers={"Origin": BASE})
    check("真实同源 Origin 放行", status != 403, f"got {status}")

    total = len(PASS) + len(FAIL)
    print(f"\n{'=' * 46}\n通过 {len(PASS)}/{total}")
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print("  - " + name)
        return 1
    print("M7 线上验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
