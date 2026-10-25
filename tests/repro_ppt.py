"""复现：chat 模式发 PPT 需求，抓模型完整输出与工具事件。"""
import json
import re
import sys
import urllib.request

BASE = "http://127.0.0.1:8020"
ADMIN_PW = re.search(r"ADMIN_PASSWORD=(.*)", open(".env", encoding="utf-8").read()).group(1).strip()


def req(path, data=None, cookie=None, stream=False):
    r = urllib.request.Request(BASE + path, method="POST" if data is not None else "GET")
    r.add_header("Content-Type", "application/json")
    if cookie:
        r.add_header("Cookie", cookie)
    body = json.dumps(data).encode() if data is not None else None
    return urllib.request.urlopen(r, body, timeout=300)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "chat"
    goal = sys.argv[2] if len(sys.argv) > 2 else "做一个4页学术汇报PPT，主题：做字的形义、功能与教学应用，标题即结论"

    login = req("/api/admin/login", {"password": ADMIN_PW})
    admin_cookie = login.headers.get("Set-Cookie", "").split(";")[0]
    inv = json.load(req("/api/admin/invites", {"count": 1, "max_uses": 5, "days": 1, "note": "repro"}, admin_cookie))
    code = inv["codes"][0]

    join = req("/api/join", {"code": code, "nickname": "复现"})
    user_cookie = join.headers.get("Set-Cookie", "").split(";")[0]

    task = json.load(req("/api/tasks", {"mode": mode, "goal": goal}, user_cookie))
    tid = task["task"]["id"]
    print(f"task={tid} mode={mode}")

    r = req(f"/api/tasks/{tid}/chat", {"text": goal}, user_cookie, stream=True)
    events = []
    for raw in r:
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            try:
                d = json.loads(line[5:])
            except Exception:
                d = {"_raw": line[5:][:200]}
            events.append((ev, d))
            if ev == "tool":
                print("TOOL:", json.dumps(d, ensure_ascii=False)[:200])
            elif ev == "done":
                print("DONE keys:", list(d.keys()))
            elif ev == "error":
                print("ERROR:", json.dumps(d, ensure_ascii=False)[:300])
    # 汇总：正文全文（看模型原始输出形态）
    text_parts = [d.get("t", "") for ev, d in events if ev in ("answer", "delta", "chunk")]
    full = "".join(text_parts) or "".join(str(d.get("t", "")) for ev, d in events if "t" in d)
    print("---- 模型正文（末 1200 字）----")
    print(full[-1200:])
    print("---- 事件类型统计 ----")
    from collections import Counter
    print(Counter(ev for ev, _ in events))


main()
