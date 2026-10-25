"""市场工具包（tool-pack）专项测试：9 个新 handler + 目录校验 + 端到端安装。

运行（需本地服务已起在 :8020）：
  C:/Users/chenquanda/.workbuddy/binaries/python/envs/default/Scripts/python.exe tests/m22_tool_packs.py

覆盖：
  单元层  9 个 handler 的正例/边界/拒绝路径（含 cn_number 往返模糊、长度守卫）
  校验层  schema 形状、required 归属、count 自洽、跨包工具名唯一
  接口层  catalog_for_prompt 能从真实目录 schema 生成参数表
  服务层  安装 → 工具页可见 → 停用下架 → 卸载（按用户门禁）
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
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


def _run(kind: str, args: dict):
    """直接调 handler（绕开工具执行器，只验纯函数逻辑）。

    调用姿势是 `(ctx, tool_input)`——与内置工具和执行器的契约一致。
    2026-10-25 之前这里写的是 `h(args, {})`，与 _make_handler 里颠倒的参数
    顺序互相抵消，所以旧契约的 bug 在单测里一直看不见；execute_tool 那条
    路（聊天/工作流真实调用）才是炸的。
    """
    sys.path.insert(0, str(ROOT))
    from app.services import market

    h = market._make_handler({"handler": {"kind": kind}})
    return asyncio.run(h({}, args))


def _expect_error(kind: str, args: dict, name: str) -> None:
    from app.services import market

    try:
        _run(kind, args)
        check(name, False, "没抛异常")
    except market.MarketError:
        check(name, True)
    except Exception as exc:  # noqa: BLE001 其他异常也算失败，但要把类型报出来
        check(name, False, f"抛了非 MarketError：{type(exc).__name__}: {exc}")


# ── 单元层：9 个 handler ────────────────────────────────

def unit_date_calc() -> None:
    r = _run("date_calc", {"start": "2026-10-19", "end": "2026-10-26"})
    check("date_calc 日差", r["value"] == 7, str(r))
    r = _run("date_calc", {"start": "2026-10-19", "end": "2026-10-26", "unit": "weekday"})
    check("date_calc 工作日差", r["value"] == 5, str(r))
    r = _run("date_calc", {"start": "2026-01-15", "end": "2026-03-20", "unit": "month"})
    check("date_calc 月差", r["value"] == 2, str(r))
    r = _run("date_calc", {"start": "2026-10-24", "days": 30})
    check("date_calc 加 30 天", r["result"] == "2026-11-23 00:00", str(r))
    r = _run("date_calc", {"start": "2026-10-24", "days": -5})
    check("date_calc 减 5 天", r["result"].startswith("2026-10-19"), str(r))
    r = _run("date_calc", {"start": "2026-10-26", "end": "2026-10-19"})
    check("date_calc 倒差为负", r["value"] == -7, str(r))
    _expect_error("date_calc", {"start": "2026-10-24", "unit": "year"}, "date_calc 拒绝非法 unit")
    _expect_error("date_calc", {"start": "昨天"}, "date_calc 拒绝坏日期")
    _expect_error("date_calc", {"start": "2026-10-24"}, "date_calc 缺 end/days 报错")
    _expect_error("date_calc", {"start": "2000-01-01", "end": "2050-01-01", "unit": "weekday"}, "date_calc 工作日跨度上限")


def unit_unit_convert() -> None:
    r = _run("unit_convert", {"value": 1, "from": "m", "to": "ft"})
    check("unit_convert 米→英尺", abs(r["result"] - 3.280839895) < 1e-6, str(r))
    r = _run("unit_convert", {"value": 100, "from": "c", "to": "f"})
    check("unit_convert 摄氏→华氏", r["result"] == 212, str(r))
    r = _run("unit_convert", {"value": 1, "from": "亩", "to": "m2"})
    check("unit_convert 亩→平方米", abs(r["result"] - 666.6667) < 1e-3, str(r))
    try:
        _run("unit_convert", {"value": 1, "from": "kg", "to": "m"})
        check("unit_convert 跨类目报错", False, "没抛异常")
    except Exception as exc:  # noqa: BLE001
        check("unit_convert 跨类目报错", "不同类目" in str(exc), str(exc)[:80])
    _expect_error("unit_convert", {"value": "abc", "from": "m", "to": "ft"}, "unit_convert 拒绝非数值")
    _expect_error("unit_convert", {"value": 1, "from": "m", "to": "斤"}, "unit_convert 未知单位")


def unit_json_format() -> None:
    r = _run("json_format", {"text": '{"b":2,"a":1}'})
    check("json_format 格式化", r["ok"] and '"a": 1' in r["text"], str(r)[:120])
    r = _run("json_format", {"text": '{"b":2,"a":1}', "action": "minify"})
    check("json_format 压缩", r["text"] == '{"b":2,"a":1}', str(r))
    r = _run("json_format", {"text": '[1,2,3]', "action": "validate"})
    check("json_format 校验", r["ok"] and r["type"] == "list", str(r))
    r = _run("json_format", {"text": '{\n"a": 1,\n"b" 2\n}'})
    check("json_format 解析失败返回行列", r.get("ok") is False and r.get("line") == 3, str(r)[:120])


def unit_regex_test() -> None:
    r = _run("regex_test", {"pattern": r"(\d{4})-(\d{2})", "text": "日期 2026-10 与 2027-01"})
    check("regex_test 匹配与分组", r["matches"] == 2 and r["sample"][0]["groups"] == ["2026", "10"], str(r)[:120])
    _expect_error("regex_test", {"pattern": "a", "text": "b", "flags": "z"}, "regex_test 拒绝非法旗标")
    _expect_error("regex_test", {"pattern": "x" * 121, "text": "b"}, "regex_test 模式长度上限")
    _expect_error("regex_test", {"pattern": "", "text": "b"}, "regex_test 空模式")
    r = _run("regex_test", {"pattern": "(unclosed", "text": "b"})
    check("regex_test 编译失败不抛错", r.get("ok") is False and "无法编译" in r.get("error", ""), str(r)[:120])
    _expect_error("regex_test", {"pattern": "a", "text": "x" * 5001}, "regex_test 文本长度上限")


def unit_md_table() -> None:
    r = _run("md_table", {"rows": [["姓名", "分数"], ["张三", 95]]})
    want = "| 姓名 | 分数 |\n| --- | --- |\n| 张三 | 95 |"
    check("md_table 基本表格", r["markdown"] == want, r["markdown"][:120])
    r = _run("md_table", {"rows": [["a|b", "c"]], "header": False, "align": "center"})
    # header=false 也要有分隔行，否则渲染出来是纯文本不是表格
    want2 = "|  |  |\n| :---: | :---: |\n| a\\|b | c |"
    check("md_table 转义与对齐", r["markdown"] == want2, r["markdown"][:120])
    r = _run("md_table", {"rows": [["只有一列", "第二列"], ["缺列的"]]})
    check("md_table 缺列补空", r["markdown"].count("\n") == 2 and r["markdown"].endswith("|  |"), r["markdown"][:160])
    _expect_error("md_table", {"rows": "不是数组"}, "md_table 拒绝字符串 rows")
    _expect_error("md_table", {"rows": [["a"], "第二行是字符串"]}, "md_table 拒绝混合行")
    _expect_error("md_table", {"rows": [[str(i) for i in range(21)]]}, "md_table 列数上限")
    _expect_error("md_table", {"rows": [["a"]], "align": "middle"}, "md_table 拒绝非法 align")


def unit_text_dedupe() -> None:
    r = _run("text_dedupe", {"text": "苹果\n香蕉\n苹果\n橘子\n香蕉"})
    check("text_dedupe 行去重保序", r["kept"] == 3 and r["removed"] == 2, str(r))
    r = _run("text_dedupe", {"text": "Apple\napple\nAPPLE", "ignore_case": True})
    check("text_dedupe 忽略大小写", r["kept"] == 1, str(r))
    r = _run("text_dedupe", {"text": "b a c a", "mode": "word", "keep_order": False})
    check("text_dedupe 词级排序", r["text"] == "a b c", str(r))
    _expect_error("text_dedupe", {"text": "x", "mode": "char"}, "text_dedupe 拒绝非法 mode")
    _expect_error("text_dedupe", {"text": "x" * 50_001}, "text_dedupe 长度守卫")


def unit_cn_number() -> None:
    cases = {
        "0": "零", "10": "十", "15": "十五", "110": "一百一十",
        "10024": "一万零二十四", "100000024": "一亿零二十四",
        "123456789": "一亿二千三百四十五万六千七百八十九",
    }
    for raw, want in cases.items():
        r = _run("cn_number", {"value": raw, "dir": "to_cn"})
        check(f"cn_number {raw}→{want}", r["result"] == want, r["result"])
    back = {
        "十五": 15, "一千零二十四": 1024, "一万零二十四": 10024,
        "一亿零二十四": 100000024, "一万亿": 10**12, "两万": 20000,
    }
    for raw, want in back.items():
        r = _run("cn_number", {"value": raw, "dir": "to_ar"})
        check(f"cn_number {raw}→{want}", r["result"] == str(want), r["result"])
    _expect_error("cn_number", {"value": "abc", "dir": "to_cn"}, "cn_number 拒绝非数字")
    _expect_error("cn_number", {"value": "一万亿零几角", "dir": "to_ar"}, "cn_number 拒绝非法汉字")

    # 往返模糊：to_cn 的输出必须能原样读回（写飞一个补零规则这里就会红）
    import random

    random.seed(24)
    bad = 0
    for _ in range(60):
        n = random.randint(0, 10**12)
        cn = _run("cn_number", {"value": str(n), "dir": "to_cn"})["result"]
        got = _run("cn_number", {"value": cn, "dir": "to_ar"})["result"]
        if int(got) != n:
            bad += 1
            print(f"       往返失败 {n} -> {cn} -> {got}")
    check("cn_number 往返模糊 60 例", bad == 0, f"{bad} 例失败")


def unit_rand_gen() -> None:
    import uuid as uuid_mod

    r = _run("rand_gen", {"kind": "uuid", "count": 3})
    check("rand_gen uuid 批量", len(r["items"]) == 3 and all(uuid_mod.UUID(x) for x in r["items"]), str(r)[:80])
    r = _run("rand_gen", {"kind": "int", "min": 5, "max": 5, "count": 4})
    check("rand_gen 定值区间", r["items"] == [5, 5, 5, 5], str(r))
    r = _run("rand_gen", {"kind": "password", "len": 20})
    check("rand_gen 口令长度", len(r["items"][0]) == 20, str(r)[:60])
    r = _run("rand_gen", {"kind": "pick", "from": ["甲", "乙"], "count": 999})
    check("rand_gen count 钳位到 100", len(r["items"]) == 100, str(len(r["items"])))
    _expect_error("rand_gen", {"kind": "dice"}, "rand_gen 拒绝非法 kind")
    _expect_error("rand_gen", {"kind": "pick", "from": []}, "rand_gen pick 空池")


def unit_encode_text() -> None:
    r = _run("encode_text", {"text": "你好，市场。", "op": "base64e"})
    back = _run("encode_text", {"text": r["result"], "op": "base64d"})
    check("encode_text base64 往返", back["result"] == "你好，市场。", back.get("result", "")[:40])
    r = _run("encode_text", {"text": "abc", "op": "md5"})
    check("encode_text md5", r["result"] == "900150983cd24fb0d6963f7d28e17f72", r["result"])
    r = _run("encode_text", {"text": "abc", "op": "sha256"})
    check("encode_text sha256", r["result"].startswith("ba7816bf8f01cfea"), r["result"][:24])
    r = _run("encode_text", {"text": "这不是base64!!", "op": "base64d"})
    check("encode_text 坏 base64 不抛错", r.get("ok") is False, str(r)[:80])
    r = _run("encode_text", {"text": "a b/c", "op": "urlenc"})
    check("encode_text url 编码", r["result"] == "a%20b%2Fc", r["result"])
    _expect_error("encode_text", {"text": "x", "op": "rot13"}, "encode_text 拒绝非法 op")
    _expect_error("encode_text", {"text": "x" * 50_001, "op": "md5"}, "encode_text 长度守卫")


# ── 校验层：目录写坏要在加载期炸，不能静默上线 ──────────

def _entry(plugin_id: str, tools: list[dict], count_ok: bool = True) -> dict:
    return {
        "id": plugin_id, "name": plugin_id, "owner": "o", "kind": "tool-pack",
        "category": "tool", "version": "1", "added": "2026-01-01",
        "description": {"zh": "x"}, "payload": {"tools": tools},
    }


def _tool(name: str, schema: dict | None = None, required: list | None = None) -> dict:
    return {
        "name": name, "label": name, "desc": "d",
        "schema": schema if schema is not None else {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
        },
        "handler": {"kind": "echo"},
    }


def unit_catalog_validation() -> None:
    from app.services import market

    sys.path.insert(0, str(ROOT))

    def _expect_entry_error(entry: dict, name: str) -> None:
        try:
            market._validate_entry(entry)
            check(name, False, "没抛异常")
        except market.MarketError:
            check(name, True)

    _expect_entry_error(_entry("a", [_tool("t", schema={"properties": {}})]), "校验拒绝非 object schema")
    _expect_entry_error(_entry("a", [_tool("t", schema={"type": "object"})]), "校验拒绝缺 properties")
    _expect_entry_error(
        _entry("a", [_tool("t", schema={"type": "object", "properties": {"text": {}}, "required": ["nope"]})]),
        "校验拒绝 required 越界",
    )
    _expect_entry_error(
        _entry("a", [_tool("t", schema={"type": "object", "properties": {}, "required": "text"})]),
        "校验拒绝 required 非数组",
    )
    ok = _entry("a", [_tool("t")])
    try:
        market._validate_entry(ok)
        check("校验放行合规条目", True)
    except market.MarketError as exc:
        check("校验放行合规条目", False, exc.message)

    # load_catalog 级：count 自洽 + 跨包工具名唯一（用临时目录文件 + 还原缓存）
    orig_path, orig_cache = market.CATALOG_PATH, market._catalog_cache
    try:
        market._catalog_cache = None
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "catalog.json"

            def _write(plugins: list, count: int) -> None:
                p.write_text(json.dumps({"count": count, "plugins": plugins}), encoding="utf-8")
                market.CATALOG_PATH = p
                market._catalog_cache = None

            _write([_entry("a", [_tool("dup")])], 99)
            try:
                market.load_catalog()
                check("校验拒绝 count 不一致", False, "没抛异常")
            except market.MarketError as exc:
                check("校验拒绝 count 不一致", "count" in exc.message, exc.message)

            _write([_entry("a", [_tool("dup")]), _entry("b", [_tool("dup")])], 2)
            try:
                market.load_catalog()
                check("校验拒绝跨包工具名重复", False, "没抛异常")
            except market.MarketError as exc:
                check("校验拒绝跨包工具名重复", "dup" in exc.message, exc.message)

            _write([_entry("a", [_tool("fine")])], 1)
            try:
                cat = market.load_catalog()
                check("校验放行自洽目录", cat["count"] == 1, str(cat["count"]))
            except market.MarketError as exc:
                check("校验放行自洽目录", False, exc.message)
    finally:
        market.CATALOG_PATH, market._catalog_cache = orig_path, orig_cache

    kinds = market.handler_kinds()
    want = {"date_calc", "unit_convert", "json_format", "regex_test", "md_table",
            "text_dedupe", "cn_number", "rand_gen", "encode_text"}
    check("handler_kinds 含全部新 kind", want <= set(kinds), str(sorted(set(kinds) - want)))


def unit_prompt_from_catalog() -> None:
    """真实目录 schema → catalog_for_prompt：模型必须看得见新工具的参数。"""
    sys.path.insert(0, str(ROOT))
    from app.services import market
    from app.services.tools import executor, registry

    entry = market.catalog_index()["tool.rand-gen"]
    tool = entry["payload"]["tools"][0]
    spec = registry.ToolSpec(
        name=tool["name"], label=tool["label"], desc=tool["desc"],
        schema=tool["schema"], requires_approval=False,
    )
    executor._HANDLERS.pop(spec.name, None)
    try:
        registry.unregister_plugin_tool(spec.name)
    except Exception:  # noqa: BLE001
        pass
    registry.register_plugin_tool(spec)
    executor._HANDLERS[spec.name] = lambda args, ctx: {"ok": True}
    try:
        text = registry.catalog_for_prompt([spec.name])
        ok = all(k in text for k in ("rand_gen", "kind", "count", "pick"))
        check("catalog_for_prompt 含新工具参数", ok, text[:160])
        names = executor.enabled_tools("m22-probe-user")
        check("enabled_tools 含新工具", spec.name in names, str(names))
    finally:
        executor._HANDLERS.pop(spec.name, None)
        registry.unregister_plugin_tool(spec.name)


# ── 服务层：安装 → 可见 → 停用 → 卸载 ──────────────────

def api_toolpack_lifecycle() -> None:
    s = Session()
    admin = Session()
    status, _ = admin.call("/api/admin/login", {"password": env("ADMIN_PASSWORD")})
    if status != 200:
        check("管理员登录（服务层）", False, str(status))
        return
    status, inv = admin.call("/api/admin/invites", {"count": 1, "max_uses": 9, "days": 7, "daily_quota": 100, "note": "m22"})
    code = inv["codes"][0] if status in (200, 201) and inv.get("codes") else ""
    status, _ = s.call("/api/join", {"code": code, "nickname": "工具包专项"})
    check("邀请码登录（服务层）", status == 200, str(status))

    status, cat = s.call("/api/market/catalog")
    toolpacks = [p for p in cat.get("plugins", []) if p["kind"] == "tool-pack"]
    check("目录 12 个工具包", len(toolpacks) == 12, str(len(toolpacks)))

    status, r = s.call("/api/market/install", {"id": "tool.date-calc"})
    check("安装 tool.date-calc", status == 200, str(r)[:120])
    _, tl = s.call("/api/tools")
    row = next((t for t in (tl.get("tools") or []) if t["name"] == "date_calc"), None)
    check("工具页可见 date_calc", bool(row) and row.get("source") == "market", str(row)[:120])
    check("date_calc schema 为标准式", bool(row) and (row.get("schema") or {}).get("type") == "object", str(row)[:120])

    status, r = s.call("/api/market/toggle", {"id": "tool.date-calc", "enabled": False})
    check("停用 tool.date-calc", status == 200, str(status))
    _, tl2 = s.call("/api/tools")
    row2 = next((t for t in (tl2.get("tools") or []) if t["name"] == "date_calc"), None)
    # 本用户是唯一安装者：停用后进程级注册被摘除，行直接消失；
    # 若还有别的用户开着，行会留在目录里但 enabled=False。两种都算下架。
    check("停用后本用户视角下架", row2 is None or row2.get("enabled") is False, str(row2)[:120])

    status, r = s.call("/api/market/install", {"id": "tool.encode-text"})
    check("安装 tool.encode-text", status == 200, str(r)[:120])
    _, tl3 = s.call("/api/tools")
    row3 = next((t for t in (tl3.get("tools") or []) if t["name"] == "encode_text"), None)
    check("第二个工具包可见 encode_text", bool(row3) and row3.get("source") == "market", str(row3)[:120])

    status, _ = s.call("/api/market/uninstall", {"id": "tool.date-calc"})
    check("卸载 tool.date-calc", status == 200, str(status))
    status, _ = s.call("/api/market/uninstall", {"id": "tool.encode-text"})
    check("卸载 tool.encode-text", status == 200, str(status))
    _, tl4 = s.call("/api/tools")
    left = [t["name"] for t in (tl4.get("tools") or []) if t["name"] in ("date_calc", "encode_text")]
    check("卸载后工具清空", not left, str(left))


def main() -> None:
    unit_date_calc()
    unit_unit_convert()
    unit_json_format()
    unit_regex_test()
    unit_md_table()
    unit_text_dedupe()
    unit_cn_number()
    unit_rand_gen()
    unit_encode_text()
    unit_catalog_validation()
    unit_prompt_from_catalog()
    api_toolpack_lifecycle()

    print(f"\n{'PASS' if not FAIL else 'FAIL'}  {len(PASS)} ok / {len(FAIL)} failed")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
