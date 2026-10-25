"""端到端：chat 模式发 PPT 需求 → 捕获 doc_generate 审批 → 自动批准 → 验证 pptx 生成。"""
import json
import re
import urllib.request

BASE = "http://127.0.0.1:8020"
ADMIN_PW = re.search(r"ADMIN_PASSWORD=(.*)", open(".env", encoding="utf-8").read()).group(1).strip()
GOAL = "做一个4页学术汇报PPT，主题：做字的形义、功能与教学应用，标题即结论"


def req(path, data=None, cookie=None, method=None):
    r = urllib.request.Request(BASE + path, method=method or ("POST" if data is not None else "GET"))
    r.add_header("Content-Type", "application/json")
    if cookie:
        r.add_header("Cookie", cookie)
    body = json.dumps(data).encode() if data is not None else None
    return urllib.request.urlopen(r, body, timeout=600)


def main():
    login = req("/api/admin/login", {"password": ADMIN_PW})
    admin_cookie = login.headers.get("Set-Cookie", "").split(";")[0]
    inv = json.load(req("/api/admin/invites", {"count": 1, "max_uses": 5, "days": 1, "note": "e2e"}, admin_cookie))
    join = req("/api/join", {"code": inv["codes"][0], "nickname": "e2e"})
    cookie = join.headers.get("Set-Cookie", "").split(";")[0]

    task = json.load(req("/api/tasks", {"mode": "chat", "goal": GOAL}, cookie))
    tid = task["task"]["id"]
    print(f"task={tid}")

    r = req(f"/api/tasks/{tid}/chat", {"text": GOAL}, cookie)
    pending = None
    for raw in r:
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            try:
                d = json.loads(line[5:])
            except Exception:
                continue
            if ev == "approval":
                pending = d
                print("APPROVAL:", d.get("name"), str(d.get("input"))[:80])
            elif ev == "tool":
                print("TOOL:", d.get("name"), d.get("status"), str(d.get("error") or "")[:60])

    if not pending:
        print("!! 没有捕获到审批请求")
        return

    print("---- 批准执行 ----")
    resp = json.load(req(f"/api/tasks/{tid}/tool-approve",
                         {"name": pending["name"], "input": pending["input"], "approved": True}, cookie))
    print("approve ok:", resp.get("ok"), "resumed:", str(resp.get("resumed"))[:200])

    detail = json.load(req(f"/api/tasks/{tid}", None, cookie))
    files = detail.get("files") or []
    print("---- 任务文件 ----")
    for f in files:
        print(f"  [{f.get('kind')}] {f.get('name')} ({f.get('size')} bytes)")
    if not any(f.get("kind") == "deliverable" for f in files):
        print("!! 没有交付物文件")


main()
