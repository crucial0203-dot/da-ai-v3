"""M8 验收：模型选择 + 附件图片链路。

两档运行：
    python tests/m8_models.py            # --fake 档：零成本，不调真上游
    python tests/m8_models.py --real     # 追加真实模型验收（消耗额度，需上游可用）

服务需以调试模式起：
    PORT=8020 DEBUG_MODE=1 FAKE_UPSTREAM=1 ./start.sh

分节：
    [0] /api/meta 模型契约
    [1] 模型切换真实生效（fake 回显）
    [2] 非法/下线模型回落，不 500
    [3] 纯文本模型 + 图片：选文件被拦、对话被拦、不扣次
    [4] 多模态真看图（--real，Pillow 造含代号样张）
    [5] 旧库加列演练（副本上做，不碰真库）
    [6] effort 档位实际影响（--real）
    [7] 前端静态契约（models.js / css / 设置页）
"""
from __future__ import annotations

import http.cookiejar
import io
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = os.environ.get("DA_BASE", "http://127.0.0.1:8020")
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "app.db"
TZ = timezone(timedelta(hours=8))
REAL = "--real" in sys.argv

PASS, FAIL = [], []
_ip_seq = [0]


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


ADMIN_PW = read_env().get("ADMIN_PASSWORD", "")


def next_ip() -> str:
    _ip_seq[0] += 1
    return f"10.30.{_ip_seq[0] // 250}.{_ip_seq[0] % 250 + 1}"


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        # 本机有 HTTP_PROXY，urllib 会拿它代理 127.0.0.1 返回 502——必须显式禁用
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar),
        )
        self.xff = next_ip()

    def _req(self, path: str, data=None, method=None, headers=None):
        req = urllib.request.Request(BASE + path, data=data, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        req.add_header("X-Forwarded-For", self.xff)
        try:
            with self.opener.open(req, timeout=120) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        status, raw = self._req(path, data=data, method=method,
                                headers={"Content-Type": "application/json"})
        try:
            return status, json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return status, raw.decode("utf-8", "replace")

    def upload(self, task_id: str, files: list[tuple[str, bytes, str]]):
        """multipart 上传。files = [(filename, content, ctype)]，字段名固定 files。"""
        boundary = uuid.uuid4().hex
        buf = io.BytesIO()
        for filename, content, ctype in files:
            buf.write(f"--{boundary}\r\n".encode())
            buf.write(
                f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'.encode()
            )
            buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
            buf.write(content)
            buf.write(b"\r\n")
        buf.write(f"--{boundary}--\r\n".encode())
        status, raw = self._req(
            f"/api/tasks/{task_id}/files", data=buf.getvalue(), method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            return status, json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return status, raw.decode("utf-8", "replace")

    def chat(self, task_id: str, body: dict):
        """发一条消息读完整个 SSE 流，返回 (events, 原始文本)。"""
        status, raw = self._req(
            f"/api/tasks/{task_id}/chat",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        if status != 200:
            try:
                return None, json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                return None, raw.decode("utf-8", "replace")
        events = []
        for block in raw.decode("utf-8", "replace").split("\n\n"):
            event, data = None, None
            for line in block.splitlines():
                if line.startswith("event: "):
                    event = line[7:].strip()
                elif line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                    except json.JSONDecodeError:
                        data = line[6:]
            if event:
                events.append((event, data))
        return events, raw.decode("utf-8", "replace")


def admin_client() -> Client:
    c = Client()
    status, _ = c.call("POST", "/api/admin/login", {"password": ADMIN_PW})
    assert status == 200, f"管理员登录失败 {status}"
    return c


def new_guest(c: Client | None = None) -> tuple[Client, str]:
    c = c or admin_client()
    code = f"M8{uuid.uuid4().hex[:6].upper()}"
    status, body = c.call("POST", "/api/admin/invites",
                          {"count": 1, "max_uses": 50, "days": 7})
    assert status == 200, f"建码失败 {status} {body}"
    first = body["codes"][0] if isinstance(body.get("codes"), list) else body["code"]
    code = first["code"] if isinstance(first, dict) else first
    g = Client()
    status, _ = g.call("POST", "/api/join", {"code": code, "nickname": "M8验收" + uuid.uuid4().hex[:4]})
    assert status == 200, f"兑换失败 {status}"
    return g, code


def calls_used(visitor_id: str) -> int:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM ai_calls WHERE visitor_id = ? "
            "AND (status='done' OR (status='reserved' AND expires_at > ?))",
            (visitor_id, datetime.now(TZ).isoformat(timespec="seconds")),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def make_task(g: Client, mode: str = "research") -> str:
    status, body = g.call("POST", "/api/tasks",
                          {"mode": mode, "title": "M8 验收任务", "goal": "验收用"})
    assert status == 200, f"建任务失败 {status} {body}"
    task = body.get("task") or body
    return task["id"]


def png_bytes(text: str, width: int = 420, height: int = 160) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=52)
    except TypeError:
        font = ImageFont.load_default()
    draw.text((24, 52), text, fill=(15, 15, 15), font=font)
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- [0] meta 契约
def section_meta() -> dict:
    print("\n[0] /api/meta 模型契约")
    c = Client()
    status, meta = c.call("GET", "/api/meta")
    check("meta 200", status == 200, str(status))
    models_list = meta.get("models") or []
    # V4.0：meta 只回 provider 有 Key 的模型。测试机可能带了别的 provider Key
    # （如系统环境里的 DEEPSEEK_API_KEY），所以断言 MiniMax 四模型在册 + 全部
    # available，不再写死总数。
    ids_set = {m.get("id", "?") for m in models_list}
    check("实测 MiniMax 四模型在册",
          {"MiniMax-M3.1-Flash", "MiniMax-M3", "MiniMax-M2.5-highspeed", "MiniMax-M2.5"} <= ids_set,
          ",".join(sorted(ids_set)))
    check("meta 只含 available 模型", all(m.get("available") for m in models_list),
          str([m["id"] for m in models_list if not m.get("available")]))
    check("默认是 M3.1-Flash",
          meta.get("default_model") == "MiniMax-M3.1-Flash", str(meta.get("default_model")))
    ids = [m.get("id") for m in models_list]
    check("M2.1 系列已下线", not any("M2.1" in i or i == "MiniMax-M2" for i in ids), str(ids))
    vision_ids = [m["id"] for m in models_list if m.get("vision")]
    check("两个多模态", set(vision_ids) == {"MiniMax-M3.1-Flash", "MiniMax-M3"}, str(vision_ids))
    check("模型条目带 effort 能力标记",
          all("effort_effective" in m and "effort_supported" in m for m in models_list))
    check("effort 档位清单下发", meta.get("efforts") == ["low", "medium", "high", "xhigh", "max"])
    upload = meta.get("upload") or {}
    check("upload 白名单下发", ".png" in upload.get("ext", []) and ".zip" not in upload.get("ext", []),
          str(upload.get("ext")))
    check("upload 带图片上限", upload.get("max_images_per_task") == 4 and upload.get("max_image_mb", 0) >= 1,
          str(upload))
    check("旧键保留（前端兼容）",
          all(k in meta for k in ("model", "max_upload_mb", "daily_calls", "modes", "capabilities")))
    return meta


# ------------------------------------------------------- [1] 模型切换真实生效
def section_switch() -> None:
    print("\n[1] 模型切换真实生效（fake 回显）")
    admin = admin_client()
    g, _ = new_guest(admin)
    task_id = make_task(g)

    # 不带 model：走默认
    events, _ = g.chat(task_id, {"text": "#fake"})
    done = next((d for e, d in events if e == "done"), None)
    meta_ev = next((d for e, d in events if e == "meta"), None)
    check("默认对话 200 且 done", bool(done), str(done)[:80])
    check("默认模型 = M3.1-Flash",
          done and done.get("model") == "MiniMax-M3.1-Flash", str(done and done.get("model")))
    check("meta 回显实际模型", meta_ev and meta_ev.get("model") == "MiniMax-M3.1-Flash",
          str(meta_ev and meta_ev.get("model")))

    # 指定快模型：上游必须真的换
    events, _ = g.chat(task_id, {"text": "#fake", "model": "MiniMax-M2.5-highspeed"})
    done = next((d for e, d in events if e == "done"), None)
    check("切换到 M2.5-highspeed 生效",
          done and done.get("model") == "MiniMax-M2.5-highspeed",
          str(done and done.get("model")))
    meta_ev = next((d for e, d in events if e == "meta"), None)
    check("meta 标识非多模态", meta_ev and meta_ev.get("vision") is False, str(meta_ev))

    # 指定另一个多模态模型
    events, _ = g.chat(task_id, {"text": "#fake", "model": "MiniMax-M3"})
    done = next((d for e, d in events if e == "done"), None)
    check("切换到 M3 生效", done and done.get("model") == "MiniMax-M3",
          str(done and done.get("model")))

    # 消息表要落 model 列
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    row = conn.execute(
        "SELECT model FROM messages WHERE task_id = ? AND role='assistant' "
        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    conn.close()
    check("assistant 消息记录了模型名", row and row[0] == "MiniMax-M3", str(row))


# ------------------------------------------------------------ [2] 非法模型回落
def section_fallback() -> None:
    print("\n[2] 非法/下线模型回落")
    admin = admin_client()
    g, _ = new_guest(admin)
    task_id = make_task(g)

    for bad in ("MiniMax-M2.1", "gpt-4o", "../../etc/passwd"):
        events, _ = g.chat(task_id, {"text": "#fake", "model": bad})
        if events is None:
            check(f"非法值「{bad}」不 500", False, str(_)[:100])
            continue
        done = next((d for e, d in events if e == "done"), None)
        meta_ev = next((d for e, d in events if e == "meta"), None)
        check(f"非法值「{bad}」回落默认且完成",
              bool(done) and done.get("model") == "MiniMax-M3.1-Flash"
              and meta_ev and meta_ev.get("model_fallback") is True,
              f"done={str(done)[:60]} fallback={meta_ev and meta_ev.get('model_fallback')}")
    # 空值 = 不指定，走默认不算回落
    events, _ = g.chat(task_id, {"text": "#fake"})
    done = next((d for e, d in events if e == "done"), None)
    check("不指定模型走默认且完成",
          bool(done) and done.get("model") == "MiniMax-M3.1-Flash", str(done)[:60])

    # 任务参数里的脏值同样要被解析层挡住
    status, _ = g.call("PATCH", f"/api/tasks/{task_id}",
                       {"params": {"model": "MiniMax-M2.1"}})
    check("PATCH 任务参数 200", status == 200, str(status))
    events, _ = g.chat(task_id, {"text": "#fake"})
    done = next((d for e, d in events if e == "done"), None)
    check("任务参数脏模型也回落", done and done.get("model") == "MiniMax-M3.1-Flash",
          str(done and done.get("model")))

    # 访客偏好：合法值要被记住，PATCH 非法值要被拒
    status, _ = g.call("PATCH", "/api/me", {"pref_model": "MiniMax-M2.5"})
    _, me = g.call("GET", "/api/me")
    check("设置默认模型偏好", status == 200 and me.get("pref_model") == "MiniMax-M2.5",
          f"{status} {str(me)[:80]}")
    status, _ = g.call("PATCH", "/api/me", {"pref_model": "MiniMax-M2.1"})
    check("偏好非法值被拒", status == 400, str(status))


# --------------------------------------------- [3] 纯文本模型 + 图片：门禁
def section_vision_gate() -> None:
    print("\n[3] 纯文本模型 + 图片门禁（不扣次）")
    admin = admin_client()
    g, _ = new_guest(admin)
    visitor_id = None
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    row = conn.execute("SELECT id FROM visitors ORDER BY created_at DESC LIMIT 1").fetchone()
    visitor_id = row[0]
    conn.close()

    task_id = make_task(g)

    # 任务默认模型是多模态，直接传图应成功且带 vision_usable
    png = png_bytes("GATE-01")
    status, body = g.upload(task_id, [("gate.png", png, "image/png")])
    check("多模态任务传图成功", status == 200 and body.get("ok"), f"{status} {str(body)[:80]}")
    file_row = (body.get("files") or [{}])[0]
    check("返回 vision_usable=true", file_row.get("vision_usable") is True, str(file_row)[:100])
    check("返回缩略图地址", bool(file_row.get("thumb_url")), str(file_row.get("thumb_url")))

    # 缩略图能取到
    status, raw = g._req(f"/api/files/{file_row['id']}/thumb")
    check("缩略图 200 且是 JPEG", status == 200 and raw[:2] == b"\xff\xd8",
          f"{status} {raw[:4]!r}")

    # 把任务参数切到纯文本模型，传第二张图必须当场被拦
    g.call("PATCH", f"/api/tasks/{task_id}", {"params": {"model": "MiniMax-M2.5"}})
    status, body = g.upload(task_id, [("gate2.png", png_bytes("GATE-02"), "image/png")])
    # M9 起 dict detail 被展开为 {detail: message, code: ...} 顶层结构
    code = body.get("code") if isinstance(body, dict) else None
    check("纯文本模型传图被 400 拦截", status == 400, f"{status} {str(body)[:80]}")
    check("拒绝码 vision_unsupported", code == "vision_unsupported", str(body)[:120])

    # 对话同样要拦（后端防线，不能只靠前端）
    events, raw = g.chat(task_id, {"text": "看看图", "model": "MiniMax-M2.5-highspeed"})
    check("纯文本模型对话被 400 拦截", events is None, str(raw)[:100])
    check("对话拒绝码 vision_unsupported",
          isinstance(raw, dict) and raw.get("code") == "vision_unsupported",
          str(raw)[:140])

    # 门禁发生在预扣之前：这位访客应当 0 次计费
    used = calls_used(visitor_id)
    check("被拦的调用不扣次", used == 0, f"ai_calls={used}")

    # 清理图片后，纯文本模型对话应放行
    status, _ = g.call("DELETE", f"/api/files/{file_row['id']}")
    check("删除图片 200", status == 200, str(status))
    events, _ = g.chat(task_id, {"text": "#fake", "model": "MiniMax-M2.5"})
    done = next((d for e, d in events if e == "done"), None)
    check("删图后纯文本模型放行", bool(done), str(done)[:60])


# --------------------------------------------- [4] 多模态真看图（--real）
def section_real_vision() -> None:
    print("\n[4] 多模态真看图（真实上游）")
    admin = admin_client()
    g, _ = new_guest(admin)
    task_id = make_task(g)
    token = f"DA-7Q{uuid.uuid4().hex[:2].upper()}"  # 每次随机两位，杜绝模型靠猜蒙对
    png = png_bytes(token)
    status, body = g.upload(task_id, [("code.png", png, "image/png")])
    check("样张上传成功", status == 200, f"{status} {str(body)[:80]}")
    if status != 200:
        return

    events, _ = g.chat(task_id, {
        "text": "图里写的代号是什么？只回答代号本身，不要解释。",
        "model": "MiniMax-M3.1-Flash", "effort": "low",
    })
    meta_ev = next((d for e, d in events if e == "meta"), None)
    check("meta 报告携带 1 张图", meta_ev and meta_ev.get("images") == 1,
          str(meta_ev and meta_ev.get("images")))
    text = "".join(d.get("t", "") for e, d in events if e == "text")
    usage = next((d for e, d in events if e == "usage"), None)
    check(f"模型回答包含代号 {token}", token in text, f"回答：{text[:120]!r}")
    check("usage 报告图片数", usage and usage.get("images") == 1, str(usage))
    check("图片真实消耗了输入 token",
          usage and int(usage.get("in") or 0) > 1000, f"in={usage and usage.get('in')}")


# --------------------------------------------- [5] 旧库加列演练
def section_migration() -> None:
    print("\n[5] 旧库加列演练（副本）")
    import shutil

    tmp = ROOT / "data" / "tmp" / f"m8mig{uuid.uuid4().hex[:6]}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(DB_PATH, tmp / "app.db")
        sys.path.insert(0, str(ROOT))
        from app import config as app_config
        from app import db as app_db

        app_config.DATA_DIR = tmp
        app_config.DB_PATH = tmp / "app.db"
        app_config.FILES_DIR = tmp / "files"

        # 真库可能已被新代码迁移过；先把两列删掉，制造一个真正的"旧库"
        conn = sqlite3.connect(str(tmp / "app.db"))
        for table, col in (("visitors", "pref_model"), ("messages", "model")):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            if col in cols:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
        conn.commit()
        before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("visitors", "tasks", "messages", "files")}
        conn.close()

        app_db.init_db()
        conn = sqlite3.connect(str(tmp / "app.db"))
        after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("visitors", "tasks", "messages", "files")}
        vcols = [r[1] for r in conn.execute("PRAGMA table_info(visitors)")]
        mcols = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
        conn.close()

        check("四张表行数不变", before == after, f"{before} -> {after}")
        check("visitors.pref_model 已加", "pref_model" in vcols)
        check("messages.model 已加", "model" in mcols)
        check("迁移结果记了 2 列",
              app_db.MIGRATIONS.get("ok") is True
              and set(app_db.MIGRATIONS.get("added") or []) == {"visitors.pref_model", "messages.model"},
              str(app_db.MIGRATIONS))
        backups = list((tmp / "backups").glob("*.db"))
        check("迁移前有备份", len(backups) == 1, str([p.name for p in backups]))

        app_db.init_db()  # 幂等
        backups = list((tmp / "backups").glob("*.db"))
        check("二次启动不再备份", len(backups) == 1, str(len(backups)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------- [6] effort 实际影响（--real）
def section_real_effort() -> None:
    print("\n[6] effort 档位实际影响（真实上游）")
    admin = admin_client()
    g, _ = new_guest(admin)
    task_id = make_task(g)
    prompt = "用大约两百字说明：行政协议诉讼中，原告对履行请求的举证责任为什么更重。"
    lens = {}
    for effort in ("low", "max"):
        events, _ = g.chat(task_id, {
            "text": prompt, "model": "MiniMax-M3.1-Flash", "effort": effort,
        })
        thinking = "".join(d.get("t", "") for e, d in events if e == "thinking")
        lens[effort] = len(thinking)
    check("meta 档位透传（low/max 都能跑）", lens["low"] >= 0 and lens["max"] >= 0, str(lens))
    check(f"max 思考量大于 low（low={lens['low']} / max={lens['max']} 字）",
          lens["max"] > lens["low"], str(lens))


# --------------------------------------------- [7] 前端静态契约
# V4.0 N2 起前端是 web/ 的 React 构建产物，旧 app/static/js|css 已删除；
# 这组 DOM 断言迁到 Playwright p2（模型胶囊/浮层/发送带参）。这里只保留
# 构建产物存在性，防止「改了前端忘了 build」就把旧产物发上去。
def section_frontend() -> None:
    print("\n[7] 前端构建产物")
    dist_index = ROOT / "app" / "static" / "ui" / "index.html"
    dist_assets = ROOT / "app" / "static" / "ui" / "bundle"
    check("ui/index.html 存在", dist_index.is_file(), str(dist_index))
    check("ui/bundle 目录存在", dist_assets.is_dir(), "")
    if dist_index.is_file():
        html = dist_index.read_text(encoding="utf-8")
        check("index 引用 /ui/bundle", "/ui/bundle/" in html, "")


def main() -> None:
    print(f"== M8 模型与图片验收（{'fake+real' if REAL else 'fake'}）==")
    section_meta()
    section_switch()
    section_fallback()
    section_vision_gate()
    section_migration()
    section_frontend()
    if REAL:
        section_real_vision()
        section_real_effort()
    else:
        print("\n（跳过 [4][6]：真实上游验收需 --real，当前 MiniMax 额度受限）")

    print(f"\n== 结果：{len(PASS)} 过 / {len(FAIL)} 败 ==")
    if FAIL:
        for name in FAIL:
            print("  !!", name)
        sys.exit(1)


if __name__ == "__main__":
    main()
