"""客户端错误上报：端点可用、落审计、限流生效、不鉴权可用。

背景：React 错误边界捕获的异常不会冒泡到 window.onerror，前端必须主动 POST
才能看见线上崩溃。这个脚本验的就是这条链路的后端一半。

运行（需本地服务已起在 :8020）：
  C:/Users/chenquanda/.workbuddy/binaries/python/envs/default/Scripts/python.exe tests/m24_client_error.py
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8020"
PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"{'  OK ' if ok else '  !! '} {name}{f'  [{detail}]' if detail else ''}")


class Session:
    def __init__(self) -> None:
        self.cookie = ""

    def call(self, path: str, body: dict | None = None) -> tuple[int, dict | str]:
        req = urllib.request.Request(
            BASE + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "Cookie": self.cookie},
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode()
                set_cookie = resp.headers.get("set-cookie")
                if set_cookie:
                    self.cookie = set_cookie.split(";")[0]
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except Exception:  # noqa: BLE001
                return e.code, raw


def env(key: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


def wait_for_window(session: "Session", timeout: int = 80) -> None:
    """限流窗口是 60 秒，脚本连跑两次必然撞上。等它清空再断言，
    否则「未登录也能上报」会因为上一次的洪水请求而假红。"""
    import time

    waited = 0
    while waited < timeout:
        _, probe = session.call("/api/client-error", {"label": "m24-probe", "message": "probe", "path": "/"})
        if isinstance(probe, dict) and probe.get("ok") is True:
            if waited:
                print(f"  （等限流窗口清空 {waited}s）")
            return
        time.sleep(5)
        waited += 5


def main() -> int:
    print("\n=== 客户端错误上报 ===")

    anon = Session()
    wait_for_window(anon)

    # 1) 不鉴权也能报——崩溃可能发生在会话建立之前
    marker = "m24-marker-" + __import__("time").strftime("%H%M%S")
    status, body = anon.call("/api/client-error", {
        "label": "m24-test",
        "message": marker,
        "stack": "at Component (http://x/y.js:1:1)",
        "path": "/t/abc",
    })
    check("未登录也能上报", status == 200 and body.get("ok") is True, f"status={status} body={body}")

    # 2) 管理员登录，查审计里有没有这条
    admin = Session()
    pwd = env("ADMIN_PASSWORD")
    status, body = admin.call("/api/admin/login", {"password": pwd})
    check("管理员登录", status == 200, f"status={status}")

    status, body = admin.call("/api/admin/audit")
    rows = body.get("items") if isinstance(body, dict) else body
    rows = rows or []
    # /api/admin/audit 返回的是已解析的 detail 字典（不是 detail_json 字符串）
    hit = [r for r in rows if isinstance(r, dict) and r.get("action") == "client_error" and marker in str(r.get("detail") or "")]
    check("审计里有 client_error 且含标记", bool(hit), f"命中 {len(hit)} 条 / 共 {len(rows)} 条")

    if hit:
        detail = hit[0].get("detail")
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except Exception:  # noqa: BLE001
                detail = {}
        detail = detail or {}
        check("审计详情含 label 与 path", detail.get("label") == "m24-test" and detail.get("path") == "/t/abc",
              f"label={detail.get('label')} path={detail.get('path')}")
        check("审计 target 是路由", hit[0].get("target") == "/t/abc", f"target={hit[0].get('target')}")
        check("审计栈信息有落库", "Component" in str(detail.get("stack") or ""), f"stack={str(detail.get('stack'))[:40]}")

    # 3) 限流：连打 40 次，必须出现 dropped
    dropped = 0
    ok = 0
    for i in range(40):
        s, b = anon.call("/api/client-error", {"label": "m24-flood", "message": f"flood-{i}", "path": "/x"})
        if isinstance(b, dict) and b.get("dropped"):
            dropped += 1
        elif s == 200:
            ok += 1
    check("限流生效（出现 dropped）", dropped > 0, f"ok={ok} dropped={dropped}")
    check("限流不是一刀切（前面放行了一批）", ok > 0, f"ok={ok}")

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        return 1
    print("CLIENT ERROR OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
