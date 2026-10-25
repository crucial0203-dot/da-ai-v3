"""V4.0 测试共享工具：登录、SSE 读取、断言收集。

m14-m19 都用它，避免每个测试文件重复写一遍 urllib + cookie 的样板。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

BASE = os.environ.get("DA_BASE", "http://127.0.0.1:8020")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"{'  OK ' if ok else '  !! '} {name}" + (f"  [{detail}]" if detail else ""))
    return ok


class Client:
    """带 cookie 的最小 HTTP 客户端。"""

    def __init__(self, base: str = BASE):
        self.base = base
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPRedirectHandler(),
        )

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else self.base + path

    def request(self, method: str, path: str, body=None, headers: dict | None = None,
                raw: bool = False):
        data = None
        hdrs = dict(headers or {})
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self._url(path), data=data, headers=hdrs, method=method)
        try:
            with self.opener.open(req, timeout=60) as resp:
                payload = resp.read()
                if raw:
                    return resp.status, payload.decode("utf-8", errors="replace"), dict(resp.headers)
                return resp.status, json.loads(payload.decode("utf-8")) if payload else {}
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            if raw:
                return exc.code, payload.decode("utf-8", errors="replace"), dict(exc.headers)
            try:
                return exc.code, json.loads(payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return exc.code, {"detail": payload.decode("utf-8", errors="replace")[:300]}

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, body=None, **kw):
        return self.request("POST", path, body=body, **kw)

    def patch(self, path: str, body=None, **kw):
        return self.request("PATCH", path, body=body, **kw)

    def delete(self, path: str, **kw):
        return self.request("DELETE", path, **kw)


def admin_login(password: str) -> Client:
    """管理员登录，返回带 cookie 的客户端。"""
    client = Client()
    status, _ = client.post("/api/admin/login", {"password": password})
    if status != 200:
        raise RuntimeError(f"管理员登录失败：{status}")
    return client


def join(client: Client, code: str, nickname: str = "m测试") -> dict:
    """用邀请码进站。"""
    status, data = client.post("/api/join", {"code": code.lower(), "nickname": nickname})
    if status != 200:
        raise RuntimeError(f"进站失败：{status} {data}")
    return data


def make_invite(admin: Client, count: int = 1, quota: int = 30, note: str = "m") -> str:
    """建邀请码，返回第一个码。"""
    return make_invites(admin, count=count, quota=quota, note=note)[0]


def make_invites(admin: Client, count: int = 1, quota: int = 30, note: str = "m") -> list[str]:
    """建多个邀请码，返回码列表。"""
    status, data = admin.post(
        "/api/admin/invites",
        {"count": count, "max_uses": 10, "days": 7, "daily_quota": quota, "note": note},
    )
    if status != 200:
        raise RuntimeError(f"建邀请码失败：{status} {data}")
    return list(data["codes"])


def sse_events(raw: str) -> list[tuple[str, dict]]:
    """把 SSE 原始文本解析成 [(event, data), ...]。"""
    out: list[tuple[str, dict]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        event = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        try:
            out.append((event, json.loads(payload)))
        except json.JSONDecodeError:
            out.append((event, {"raw": payload}))
    return out


def stream(client: Client, path: str, body: dict) -> list[tuple[str, dict]]:
    """发一个 SSE 请求并读完整响应。"""
    status, raw, _ = client.request("POST", path, body=body, raw=True)
    if status != 200:
        return [("__http_error__", {"status": status, "body": raw[:400]})]
    return sse_events(raw)


def summary() -> int:
    """打印汇总并返回退出码。"""
    print()
    print("=" * 46)
    print(f"通过 {len(PASS)}/{len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项：", FAIL)
        return 1
    return 0
