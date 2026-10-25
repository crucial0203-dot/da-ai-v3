"""插件市场服务 v0.2 —— 移植自 dsh-market（DSH 插件市场）的目录-安装-启停模型。

与源版的对应关系（详见 docs/PLUGIN-MARKET.md）：
  - 社区目录 plugins.json（registry.ts）   → app/data/market/catalog.json（打包内置快照）
  - 安装：拒装不在目录的 + 事务回滚       → install() 只认目录条目，DB 事务写入
  - 热开关 cordis.patch.yml + HMR          → market_plugins.enabled + 前端可逆注册即时生效
  - 主题 ThemeDef tokens 互斥激活          → kind=theme 启用时互斥停用其他主题
  - 操作队列 + 一步回滚                    → market_ops 审计表 + undo_last()

三类可安装插件：
  theme      payload.colorScheme + payload.tokens（CSS 变量覆写，前端即时换肤）
  ui-module  payload.module（app/data/market/modules/*.js 源码，前端动态加载 → activate(ctx)）
  tool-pack  payload.tools[]（工具声明 + 通用 handler，注册进后端工具目录）

安装状态按用户隔离（user_id）；tool-pack 启用后工具是进程级注册，属已知简化。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .. import db
from . import plugins as plugins_service
from .tools import registry as tools_registry

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "market"
CATALOG_PATH = DATA_DIR / "catalog.json"

_KINDS = {"theme", "ui-module", "tool-pack", "dsh-ref"}
_catalog_cache: dict | None = None
# 进程内已激活的工具包：plugin_id -> PluginContext（可逆注册）。
#
# 按 plugin_id 记，不按 (user_id, plugin_id)：工具是进程级注册的（已知简化，
# 所有用户互见），按用户记会撞「第二个用户装同一个包 → 重复注册 → 抛 ValueError
# → 装包 500」，而且前一个用户卸载会把别人还在用的工具一起摘掉（2026-10-24 修）。
_active_toolpacks: dict[str, plugins_service.PluginContext] = {}


class MarketError(Exception):
    """市场操作错误（message 可直接回给前端）。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ── 目录 ──────────────────────────────────────────────

def _validate_entry(entry: dict) -> None:
    for field in ("id", "name", "owner", "kind", "category", "version", "added"):
        if not isinstance(entry.get(field), str) or not entry[field]:
            raise MarketError(f"目录条目缺必填字段 {field}")
    if entry["kind"] not in _KINDS:
        raise MarketError(f"未知插件类型 {entry['kind']}")
    desc = entry.get("description")
    if not isinstance(desc, dict) or not desc.get("zh"):
        raise MarketError(f"目录条目 {entry['id']} 缺中文描述")
    # dsh-ref：DSH 社区目录的浏览型条目——不可安装，无 payload，
    # 必须 url 指向公网 http(s)（一般成 https://github.com/<owner>/<name>）。
    if entry["kind"] == "dsh-ref":
        url = entry.get("url")
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            raise MarketError(f"DSH 条目 {entry['id']} 缺合法的 url（http/https）")
        if host in {"localhost"} or host.startswith(("127.", "10.", "192.168.", "169.254.")):
            raise MarketError(f"DSH 条目 {entry['id']} 的 url 指向内网，拒绝")
        caps = entry.get("capabilities")
        if caps is not None and not (isinstance(caps, list) and all(isinstance(c, str) for c in caps)):
            raise MarketError(f"DSH 条目 {entry['id']} 的 capabilities 必须是字符串数组")
        reds = entry.get("capabilityRedLines")
        if reds is not None and not (isinstance(reds, list) and all(isinstance(c, str) for c in reds)):
            raise MarketError(f"DSH 条目 {entry['id']} 的 capabilityRedLines 必须是字符串数组")
        return
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        raise MarketError(f"目录条目 {entry['id']} 缺 payload")
    if entry["kind"] == "theme":
        if not isinstance(payload.get("tokens"), dict) or not payload["tokens"]:
            raise MarketError(f"主题 {entry['id']} 缺 tokens")
    elif entry["kind"] == "ui-module":
        if not payload.get("module"):
            raise MarketError(f"UI 模块 {entry['id']} 缺 module 路径")
    elif entry["kind"] == "tool-pack":
        tools = payload.get("tools")
        if not isinstance(tools, list) or not tools:
            raise MarketError(f"工具包 {entry['id']} 缺 tools")
        for t in tools:
            if not t.get("name") or not t.get("handler", {}).get("kind"):
                raise MarketError(f"工具包 {entry['id']} 的工具声明不完整")
            kind = t["handler"]["kind"]
            if kind not in _TOOL_HANDLERS:
                raise MarketError(
                    f"工具包 {entry['id']} 的 handler.kind「{kind}」不在已知集合内：{handler_kinds()}"
                )
            # schema 形状：模型按 catalog_for_prompt 读它，写坏等于给模型发假参数表
            schema = t.get("schema")
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise MarketError(
                    f"工具包 {entry['id']} 的工具 {t['name']} 的 schema 必须是 type=object"
                )
            props = schema.get("properties")
            if not isinstance(props, dict):
                raise MarketError(
                    f"工具包 {entry['id']} 的工具 {t['name']} 的 schema 缺 properties"
                )
            required = schema.get("required", [])
            if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
                raise MarketError(
                    f"工具包 {entry['id']} 的工具 {t['name']} 的 required 必须是字符串数组"
                )
            for r in required:
                if r not in props:
                    raise MarketError(
                        f"工具包 {entry['id']} 的工具 {t['name']} 的 required「{r}」不在 properties 里"
                    )


def load_catalog() -> dict:
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    seen: set[str] = set()
    seen_tools: dict[str, str] = {}  # 工具名 -> 所属插件 id
    for entry in raw.get("plugins", []):
        _validate_entry(entry)
        if entry["id"] in seen:
            raise MarketError(f"目录插件 id 重复：{entry['id']}")
        seen.add(entry["id"])
        if entry["kind"] == "tool-pack":
            for tool in (entry.get("payload") or {}).get("tools", []):
                name = tool.get("name")
                if not name:
                    continue
                # 工具名进的是进程级全局注册表：两个包声明同一个名字，
                # 后装的会被 _sync_toolpack 静默跳过——装了个什么都不干的包。
                # 加载期就拦，比运行时静默好。
                if name in seen_tools:
                    raise MarketError(
                        f"工具名「{name}」被 {seen_tools[name]} 与 {entry['id']} 重复声明"
                    )
                seen_tools[name] = entry["id"]
    declared = raw.get("count")
    if isinstance(declared, int) and declared != len(raw.get("plugins", [])):
        raise MarketError(
            f"目录 count={declared} 与实际插件数 {len(raw.get('plugins', []))} 不一致"
        )
    raw["count"] = len(raw.get("plugins", []))
    _catalog_cache = raw
    return raw


def catalog_index() -> dict[str, dict]:
    return {p["id"]: p for p in load_catalog().get("plugins", [])}


def entry_payload(entry: dict) -> dict:
    """展开 payload：ui-module 的源码从文件读出内联，供安装快照与前端动态加载。"""
    payload = dict(entry.get("payload") or {})
    if entry["kind"] == "ui-module":
        src_path = DATA_DIR / payload["module"]
        if not src_path.exists():
            raise MarketError(f"UI 模块源码缺失：{payload['module']}", 404)
        payload["source"] = src_path.read_text(encoding="utf-8")
    return payload


# ── 安装状态（按用户） ─────────────────────────────────

def installed_rows(user_id: str) -> list[dict]:
    rows = db.rows(
        "SELECT id, kind, name, version, manifest_json, enabled, installed_at, updated_at "
        "FROM market_plugins WHERE user_id = ? ORDER BY installed_at DESC",
        (user_id,),
    )
    return [dict(r) for r in rows]


def installed_index(user_id: str) -> dict[str, dict]:
    return {r["id"]: r for r in installed_rows(user_id)}


def get_installed(user_id: str, plugin_id: str) -> dict | None:
    row = db.one(
        "SELECT id, kind, name, version, manifest_json, enabled, installed_at, updated_at "
        "FROM market_plugins WHERE user_id = ? AND id = ?",
        (user_id, plugin_id),
    )
    return dict(row) if row else None


def active_theme_id(user_id: str) -> str | None:
    row = db.one(
        "SELECT id FROM market_plugins WHERE user_id = ? AND kind = 'theme' AND enabled = 1",
        (user_id,),
    )
    return row["id"] if row else None


# ── 工具包注册（进程级，可逆） ──────────────────────────

def _safe_calc(expression: str) -> float:
    if not re.fullmatch(r"[\d\s+\-*/().%]+", expression or ""):
        raise MarketError("表达式含非法字符，只允许数字与 + - * / ( ) % .")
    if len(expression) > 200:
        raise MarketError("表达式过长")
    try:
        value = eval(compile(expression, "<calc>", "eval"), {"__builtins__": {}}, {})  # noqa: S307 白名单字符集
    except Exception as exc:  # noqa: BLE001
        raise MarketError(f"表达式无法计算：{exc}") from exc
    if not isinstance(value, (int, float)):
        raise MarketError("计算结果不是数值")
    return value


# ── 工具包 handler 分派表 ───────────────────────────────
#
# 单一事实源：目录里 handler.kind 的合法取值就是这张表的键——目录校验直接拿
# 它当白名单，写错 kind 就在加载期报错，而不是静默回落 echo
# （装了一个「什么都不做」的工具包，最难排查）。
#
# 三条纪律（本进程是单进程，一个坏 handler 能把整站拖住）：
#   1. 只用标准库，无 I/O、无 exec；calc 是唯一例外，走 _safe_calc 的
#      字符集白名单 + 空内置环境；
#   2. 每个 handler 开头做输入长度守卫，超限抛 MarketError 回注给模型；
#   3. 纯同步即可——由 _make_handler 的 async 包装调用，不引入阻塞 I/O。
_TOOL_HANDLERS: dict[str, Callable[[dict], Any]] = {}


def _tool_handler(kind: str):
    def deco(fn: Callable[[dict], Any]):
        _TOOL_HANDLERS[kind] = fn
        return fn

    return deco


def handler_kinds() -> list[str]:
    """合法的 handler.kind 全集（目录校验与前端展示共用）。"""
    return sorted(_TOOL_HANDLERS)


@_tool_handler("time")
def _h_time(args: dict) -> Any:
    from datetime import datetime

    now = datetime.now()
    return {
        "now": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "weekday": now.isoweekday(),
    }


@_tool_handler("calc")
def _h_calc(args: dict) -> Any:
    return {"expression": args.get("expression", ""), "result": _safe_calc(str(args.get("expression", "")))}


@_tool_handler("text_stats")
def _h_text_stats(args: dict) -> Any:
    text = str(args.get("text", ""))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return {
        "chars": len(text),
        "chars_no_space": len(re.sub(r"\s", "", text)),
        "words": len(text.split()),
        "lines": text.count("\n") + 1 if text else 0,
        "cjk_chars": cjk,
        "cjk_ratio": round(cjk / len(text), 4) if text else 0,
    }


@_tool_handler("template")
def _h_template(args: dict) -> Any:
    template = str(args.get("template", ""))
    fields = args.get("fields") or {}
    if not isinstance(fields, dict):
        raise MarketError("fields 必须是对象")
    if len(template) > 4000:
        raise MarketError("模板过长")
    return {"text": template.format_map({k: str(v) for k, v in fields.items()})}


@_tool_handler("echo")
def _h_echo(args: dict) -> Any:
    return {"echo": args}


# ── 第二批工具包（2026-10-24 扩）：全部纯标准库、纯计算 ──
#
# 每个 handler 开头都做长度守卫：本服务是单进程，一个没上限的 O(n) handler
# 就足够把整站拖住（模型很可能把整篇文档塞进 text 参数）。

_MAX_TEXT = 50_000


def _guard_text(value: Any, limit: int = _MAX_TEXT, name: str = "text") -> str:
    text = str(value or "")
    if len(text) > limit:
        raise MarketError(f"{name} 过长（上限 {limit} 字，收到 {len(text)} 字）")
    return text


@_tool_handler("date_calc")
def _h_date_calc(args: dict) -> Any:
    """日期差（日/工作日/月）或日期加减天数。"""
    from datetime import datetime, timedelta

    def parse(value: Any) -> datetime:
        raw = str(value or "").strip()
        if not raw or len(raw) > 30:
            raise MarketError("日期为空或过长，用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM")
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        raise MarketError(f"日期无法识别：{raw}（用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM）")

    unit = str(args.get("unit") or "day")
    if unit not in ("day", "weekday", "month"):
        raise MarketError("unit 只能是 day / weekday / month")

    end = args.get("end")
    if end not in (None, ""):
        start, stop = parse(args.get("start")), parse(end)
        sign = 1 if stop >= start else -1
        lo, hi = (start, stop) if sign > 0 else (stop, start)
        if unit == "day":
            value = (hi - lo).days
        elif unit == "month":
            value = (hi.year - lo.year) * 12 + (hi.month - lo.month)
        else:  # weekday：逐日走一遍，跨度太大就不算
            span = (hi - lo).days
            if span > 3650:
                raise MarketError("日期跨度过大（工作日计算上限 10 年）")
            value = sum(
                1 for i in range(span) if (lo + timedelta(days=i)).weekday() < 5
            )
        return {"start": str(start), "end": str(stop), "unit": unit, "value": sign * value}

    days = args.get("days")
    if days is None:
        raise MarketError("要么给 end（算两个日期的差），要么给 days（算加减后的日期）")
    try:
        days = int(days)
    except (TypeError, ValueError) as exc:
        raise MarketError("days 必须是整数（可为负）") from exc
    if abs(days) > 100_000:
        raise MarketError("days 超出范围（±100000）")
    start = parse(args.get("start"))
    result = start + timedelta(days=days)
    return {"start": str(start), "days": days, "result": result.strftime("%Y-%m-%d %H:%M"), "weekday": result.isoweekday()}


# 单位换算：同一类目内按「到基准单位的系数」互转
_UNIT_TABLE: dict[str, dict[str, float]] = {
    "length": {"mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0, "inch": 0.0254, "ft": 0.3048, "mile": 1609.344},
    "weight": {"mg": 1e-6, "g": 0.001, "kg": 1.0, "t": 1000.0, "lb": 0.45359237, "oz": 0.0283495},
    "area": {"m2": 1.0, "km2": 1e6, "cm2": 1e-4, "亩": 666.6667, "hectare": 10000.0, "acre": 4046.8564224},
    "volume": {"ml": 0.001, "l": 1.0, "m3": 1000.0, "cm3": 0.001, "gal": 3.785411784},
    "time": {"s": 1.0, "min": 60.0, "h": 3600.0, "day": 86400.0, "week": 604800.0},
    "data": {"b": 1.0, "kb": 1024.0, "mb": 1024.0**2, "gb": 1024.0**3, "tb": 1024.0**4},
}
# 温度是仿射变换，不能走系数表
_TEMP = {"c": "c", "f": "f", "k": "k"}


@_tool_handler("unit_convert")
def _h_unit_convert(args: dict) -> Any:
    value = args.get("value")
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise MarketError("value 必须是数字") from exc
    src = str(args.get("from") or "").strip().lower()
    dst = str(args.get("to") or "").strip().lower()
    if not src or not dst:
        raise MarketError("from 与 to 都要给")

    if src in _TEMP or dst in _TEMP:
        if src not in _TEMP or dst not in _TEMP:
            raise MarketError("温度只能和温度互转（c / f / k）")
        if src == dst:
            result = value
        elif src == "c" and dst == "f":
            result = value * 9 / 5 + 32
        elif src == "f" and dst == "c":
            result = (value - 32) * 5 / 9
        elif src == "c" and dst == "k":
            result = value + 273.15
        elif src == "k" and dst == "c":
            result = value - 273.15
        elif src == "f" and dst == "k":
            result = (value - 32) * 5 / 9 + 273.15
        else:
            result = (value - 273.15) * 9 / 5 + 32
        return {"value": value, "from": src, "to": dst, "result": round(result, 6), "category": "temperature"}

    for category, table in _UNIT_TABLE.items():
        if src in table and dst in table:
            result = value * table[src] / table[dst]
            return {"value": value, "from": src, "to": dst, "result": round(result, 10), "category": category}
        if src in table or dst in table:
            # 一边命中一边不命中 = 跨类目，直接说清楚
            hit = category
            others = sorted({u for c, t in _UNIT_TABLE.items() if c != hit for u in t})
            raise MarketError(
                f"「{src}」和「{dst}」不同类目，不能互转。"
                f"{hit} 类可用单位：{sorted(_UNIT_TABLE[hit])}；其他类目示例：{others[:12]}"
            )
    raise MarketError(f"未知单位：{src} 或 {dst}（支持 {sorted({u for t in _UNIT_TABLE.values() for u in t})} 与 温度 c/f/k）")


@_tool_handler("json_format")
def _h_json_format(args: dict) -> Any:
    """格式化 / 压缩 / 校验 JSON。解析失败返回 ok:false，不抛——数据问题是常态。"""
    text = _guard_text(args.get("text"))
    action = str(args.get("action") or "format")
    if action not in ("format", "minify", "validate"):
        raise MarketError("action 只能是 format / minify / validate")
    try:
        indent = int(args.get("indent", 2))
    except (TypeError, ValueError):
        indent = 2
    indent = max(0, min(indent, 8))
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"JSON 解析失败：{exc.msg}", "line": exc.lineno, "column": exc.colno}
    if action == "validate":
        return {"ok": True, "type": type(data).__name__, "valid": True}
    if action == "minify":
        return {"ok": True, "text": json.dumps(data, ensure_ascii=False, separators=(",", ":"))}
    out = json.dumps(data, ensure_ascii=False, indent=indent)
    if len(out) > _MAX_TEXT:
        return {"ok": True, "text": out[:_MAX_TEXT], "truncated": True}
    return {"ok": True, "text": out}


@_tool_handler("regex_test")
def _h_regex_test(args: dict) -> Any:
    """正则测试。两个上限是防灾难性回溯的唯一手段——Python re 没有超时机制。"""
    pattern = str(args.get("pattern") or "")
    if not pattern:
        raise MarketError("pattern 不能为空")
    if len(pattern) > 120:
        raise MarketError("pattern 过长（上限 120 字符）")
    text = _guard_text(args.get("text"), 5_000)
    flags = str(args.get("flags") or "")
    bits = 0
    for ch in flags:
        if ch == "i":
            bits |= re.IGNORECASE
        elif ch == "m":
            bits |= re.MULTILINE
        elif ch == "s":
            bits |= re.DOTALL
        elif ch == "x":
            bits |= re.VERBOSE
        else:
            raise MarketError(f"不支持的正则旗标 {ch}（支持 i / m / s / x）")
    try:
        compiled = re.compile(pattern, bits)
    except re.error as exc:
        return {"ok": False, "error": f"正则无法编译：{exc}"}
    found = list(compiled.finditer(text))
    out = []
    for m in found[:20]:
        out.append({"match": m.group(0), "start": m.start(), "groups": list(m.groups())})
    return {"ok": True, "pattern": pattern, "matches": len(found), "sample": out, "truncated": len(found) > 20}


@_tool_handler("md_table")
def _h_md_table(args: dict) -> Any:
    rows = args.get("rows")
    if not isinstance(rows, list) or not rows:
        raise MarketError("rows 必须是非空二维数组")
    # 每一行都必须是数组：字符串行的 len 会按字符数撑出幽灵列，
    # 渲染出来是几张空单元格，最难排查的那种脏数据
    if not all(isinstance(r, list) for r in rows):
        raise MarketError("rows 的每一行都必须是数组，不能混入字符串等其他类型")
    if len(rows) > 500:
        raise MarketError("行数过多（上限 500）")
    align = str(args.get("align") or "none")
    if align not in ("left", "center", "right", "none"):
        raise MarketError("align 只能是 left / center / right / none")
    header = args.get("header")
    header = True if header is None else bool(header)

    def cell(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    width = max(len(r) for r in rows)
    if width > 20:
        raise MarketError("列数过多（上限 20）")
    grid = [[cell(r[i]) if i < len(r) else "" for i in range(width)] for r in rows]
    marker = {"left": ":---", "center": ":---:", "right": "---:", "none": "---"}[align]

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    lines: list[str] = []
    if header:
        lines.append(line(grid[0]))
        lines.append("| " + " | ".join([marker] * width) + " |")
        lines.extend(line(r) for r in grid[1:])
    else:
        # GFM 表格必须有分隔行：header=false 的正确语义是「没有表头标签」，
        # 不是「没有分隔行」——少一行渲染出来就是纯文本，不是表格
        lines.append("| " + " | ".join([""] * width) + " |")
        lines.append("| " + " | ".join([marker] * width) + " |")
        lines.extend(line(r) for r in grid)
    return {"markdown": "\n".join(lines), "rows": len(grid), "cols": width}


@_tool_handler("text_dedupe")
def _h_text_dedupe(args: dict) -> Any:
    text = _guard_text(args.get("text"))
    mode = str(args.get("mode") or "line")
    if mode not in ("line", "word"):
        raise MarketError("mode 只能是 line / word")
    ignore_case = bool(args.get("ignore_case"))
    keep_order = args.get("keep_order")
    keep_order = True if keep_order is None else bool(keep_order)

    if mode == "line":
        items = text.splitlines()
        joiner = "\n"
    else:
        items = text.split()
        joiner = " "

    seen: set[str] = set()
    kept: list[str] = []
    for item in items:
        key = item.lower() if ignore_case else item
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    if not keep_order:
        kept.sort()
    return {"text": joiner.join(kept), "kept": len(kept), "removed": len(items) - len(kept)}


_CN_DIGITS = "零一二三四五六七八九"
_CN_UNITS = ["", "十", "百", "千"]
_CN_SECTIONS = ["", "万", "亿", "万亿"]
_CN_NUM = {c: i for i, c in enumerate(_CN_DIGITS)}
# 口语与文书里的常见变体：两=二（两百/两万），〇=零（二〇二六）
_CN_NUM["两"] = 2
_CN_NUM["〇"] = 0
_CN_UNIT_VAL = {"十": 10, "百": 100, "千": 1000}
_CN_SEC_VAL = {"亿": 100_000_000}  # 万在解析里走 wan 累加（万亿=万×亿），不查表


def _arabic_to_cn(num: int) -> str:
    """阿拉伯数字 → 中文数字。

    两个容易写错的点（都有测试钉住）：
      ① 节间补零：一万零二十四，不是「一万二十四」——低位节不足 1000 时，
         它与高位节之间要补一个零；整节为零时也一样（一亿零二十四）。
      ② 节内领头省一：「一十几」读作「十几」（15→十五），但「一百一十」
         不省（110→一百一十）。
    """
    if num == 0:
        return "零"
    if num < 0:
        return "负" + _arabic_to_cn(-num)
    secs: list[int] = []
    n = num
    while n > 0:
        secs.append(n % 10000)
        n //= 10000
    out = ""
    for i in range(len(secs) - 1, -1, -1):
        sec = secs[i]
        if sec == 0:
            continue  # 整节为零：由下一个非零节的补零规则兜底
        parts: list[str] = []
        pending_zero = False
        s = sec
        for j in range(4):
            digit = s % 10
            s //= 10
            if digit == 0:
                if parts:
                    pending_zero = True
            else:
                if pending_zero:
                    parts.append("零")
                    pending_zero = False
                parts.append(_CN_DIGITS[digit] + _CN_UNITS[j])
        sec_str = "".join(reversed(parts))
        if sec_str.startswith("一十"):
            sec_str = sec_str[1:]
        if out and sec < 1000:
            out += "零"
        out += sec_str + _CN_SECTIONS[i]
    return out


def _cn_to_arabic(text: str) -> int:
    """中文数字 → 阿拉伯数字。

    按「节」累加：个/十/百/千进 section，万把 section 收进 wan，
    亿把 wan 整体乘 1e8——一万亿 = 1万 × 1亿 = 1e12，逐字硬乘会算成 1e8。
    """
    total = 0   # 亿级及以上
    wan = 0     # 万级（已收拢的 万 以下部分）
    section = 0 # 当前节内（十/百/千）
    number = 0  # 待处理的当前数字
    for ch in text:
        if ch in _CN_NUM:
            number = _CN_NUM[ch]
        elif ch in _CN_UNIT_VAL:
            section += (number or 1) * _CN_UNIT_VAL[ch]
            number = 0
        elif ch == "万":
            wan += section + number
            section, number = 0, 0
        elif ch == "亿":
            total += (wan * 10000 + section + number) * _CN_SEC_VAL["亿"]
            wan, section, number = 0, 0, 0
        else:
            raise MarketError(f"无法识别的数字字符：{ch}")
    return total + wan * 10000 + section + number


@_tool_handler("cn_number")
def _h_cn_number(args: dict) -> Any:
    raw = str(args.get("value") or "").strip()
    if not raw or len(raw) > 200:
        raise MarketError("value 为空或过长（上限 200 字符）")
    direction = str(args.get("dir") or "to_cn")
    if direction not in ("to_cn", "to_ar"):
        raise MarketError("dir 只能是 to_cn / to_ar")
    if direction == "to_cn":
        cleaned = raw.replace(",", "")
        if not cleaned.lstrip("-").isdigit():
            raise MarketError(f"转中文数字需要纯阿拉伯数字，收到：{raw}")
        num = int(cleaned)
        if abs(num) >= 10_000_000_000_000:
            raise MarketError("数值超出支持范围（上限 万亿）")
        return {"value": raw, "result": _arabic_to_cn(num)}
    cleaned = raw.replace("负", "")
    if not cleaned:
        raise MarketError("转阿拉伯数字需要中文数字，内容为空")
    try:
        num = _cn_to_arabic(cleaned)
    except MarketError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MarketError(f"中文数字解析失败：{exc}") from exc
    return {"value": raw, "result": ("-" + str(num)) if raw.startswith("负") else str(num)}


@_tool_handler("rand_gen")
def _h_rand_gen(args: dict) -> Any:
    """随机生成。口令走 secrets（密码学安全），其余走 random 即可。"""
    import random
    import secrets
    import string
    import uuid as _uuid

    kind = str(args.get("kind") or "uuid")
    if kind not in ("uuid", "int", "password", "pick"):
        raise MarketError("kind 只能是 uuid / int / password / pick")
    count = args.get("count")
    try:
        count = 1 if count is None else int(count)
    except (TypeError, ValueError) as exc:
        raise MarketError("count 必须是整数") from exc
    count = max(1, min(count, 100))

    if kind == "uuid":
        return {"kind": kind, "items": [str(_uuid.uuid4()) for _ in range(count)]}
    if kind == "int":
        try:
            low = int(args.get("min", 1))
            high = int(args.get("max", 100))
        except (TypeError, ValueError) as exc:
            raise MarketError("min / max 必须是整数") from exc
        if low > high:
            low, high = high, low
        return {"kind": kind, "min": low, "max": high, "items": [random.randint(low, high) for _ in range(count)]}
    if kind == "password":
        try:
            length = int(args.get("len", 16))
        except (TypeError, ValueError) as exc:
            raise MarketError("len 必须是整数") from exc
        length = max(4, min(length, 64))
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
        return {"kind": kind, "len": length, "items": [
            "".join(secrets.choice(alphabet) for _ in range(length)) for _ in range(count)
        ]}
    pool = args.get("from")
    if not isinstance(pool, list) or not pool:
        raise MarketError("pick 需要 from 是一个非空数组")
    if len(pool) > 1000:
        raise MarketError("from 过长（上限 1000 项）")
    return {"kind": kind, "pool": len(pool), "items": [random.choice(pool) for _ in range(count)]}


@_tool_handler("encode_text")
def _h_encode_text(args: dict) -> Any:
    import base64
    import hashlib
    from urllib.parse import quote, unquote

    text = _guard_text(args.get("text"))
    op = str(args.get("op") or "base64e")
    if op not in ("base64e", "base64d", "urlenc", "urldec", "md5", "sha256"):
        raise MarketError("op 只能是 base64e / base64d / urlenc / urldec / md5 / sha256")
    raw = text.encode("utf-8")
    if op == "base64e":
        return {"op": op, "result": base64.b64encode(raw).decode("ascii")}
    if op == "base64d":
        try:
            return {"op": op, "result": base64.b64decode(text, validate=True).decode("utf-8")}
        except Exception as exc:  # noqa: BLE001 解码失败是常态，回给模型让它换个输入
            return {"ok": False, "op": op, "error": f"Base64 解码失败：{exc}"}
    if op == "urlenc":
        return {"op": op, "result": quote(text, safe="")}
    if op == "urldec":
        return {"op": op, "result": unquote(text)}
    if op == "md5":
        return {"op": op, "result": hashlib.md5(raw).hexdigest()}  # noqa: S324 摘要用途
    return {"op": op, "result": hashlib.sha256(raw).hexdigest()}


def _make_handler(tool: dict):
    """把一个工具声明包成执行器要求的 async handler。

    签名契约与内置工具一致：`(ctx, tool_input)`。

    以前写成 `(args, _ctx)`——顺序反了。市场工具包从装上第一天起就把 ctx
    当入参、把入参当 ctx：calc 报「表达式含非法字符」（拿到的是 ctx），
    text_stats 之类不碰参数的则**静默返回空统计**。m22 的 handler 单测是
    直调 `_TOOL_HANDLERS`，绕过了 execute_tool，所以一直没暴露
    （2026-10-25 修，m30 端到端覆盖）。
    """
    kind = (tool.get("handler") or {}).get("kind", "echo")
    fn = _TOOL_HANDLERS.get(kind)

    async def handler(ctx: dict, tool_input: dict) -> Any:
        if fn is None:  # 目录校验已拦，这里只是兜底
            return {"echo": tool_input}
        return fn(tool_input or {})

    return handler


def _sync_toolpack(plugin_id: str) -> dict:
    """按「还有没有人启用着」同步这个工具包的全局注册。

    启用条件：DB 里该插件仍有任意一条 enabled=1 的安装记录。没人启用就摘掉。
    不按用户维度追踪——工具本来就是进程级共享的，按用户记只会带来重复注册
    与「A 卸载把 B 的工具也摘了」两类 bug。

    返回 {"registered": n, "skipped": [...]}：skipped 是名字被占、装不上去的工具。
    全部被占 = 空包，抛 MarketError 让安装整体回滚（装个什么都不干的包，
    比明确告诉用户「这个包与现有工具重名了」更糟）。
    """
    entry = catalog_index().get(plugin_id)
    if not entry:
        return {"registered": 0, "skipped": []}
    still = db.one(
        "SELECT 1 FROM market_plugins WHERE id = ? AND kind = 'tool-pack' AND enabled = 1 LIMIT 1",
        (plugin_id,),
    )
    if still:
        if plugin_id in _active_toolpacks:
            return {"registered": len(_active_toolpacks[plugin_id].tools), "skipped": []}
        payload = entry_payload(entry)
        info = plugins_service.PluginInfo(
            id=f"market.{plugin_id}",
            name=entry["name"],
            version=entry["version"],
            description=entry.get("description", {}).get("zh", ""),
        )
        ctx = plugins_service.PluginContext(info)
        skipped: list[str] = []
        for tool in payload.get("tools", []):
            # 预检再注册：名字被内置工具或别的插件占了就跳过——别让一个冲突
            # 把整包装不上。注意 register_plugin_tool 对已占用的名字直接抛
            # ValueError，那时本包先前已注册的工具会被 plugins.py 的整包回滚
            # 撤掉；预检能把这条路走到头的概率降到 0。
            if tool["name"] in tools_registry.ALLOWED_TOOLS:
                skipped.append(tool["name"])
                continue
            spec = tools_registry.ToolSpec(
                name=tool["name"],
                label=tool.get("label", tool["name"]),
                desc=tool.get("desc", ""),
                schema=tool.get("schema", {}),
                requires_approval=False,
            )
            ctx.register_tool(spec, _make_handler(tool))
        if not ctx.tools:
            # 空包：撤销已注册的（理论为空）并让安装失败
            ctx.dispose()
            raise MarketError(
                f"工具包 {plugin_id} 的所有工具都已被占用（{', '.join(skipped)}），装上是空包",
                409,
            )
        _active_toolpacks[plugin_id] = ctx
        if skipped:
            print(f"[market] 工具包 {plugin_id} 跳过了被占用的工具：{skipped}", flush=True)
        return {"registered": len(ctx.tools), "skipped": skipped}
    ctx = _active_toolpacks.pop(plugin_id, None)
    if ctx is not None:
        ctx.dispose()
    return {"registered": 0, "skipped": []}


def boot_installed(user_ids: list[str]) -> int:
    """启动时恢复已启用的工具包（UI/主题由前端自己恢复）。"""
    ok = 0
    pids: list[str] = []
    for user_id in user_ids:
        for row in installed_rows(user_id):
            if not row["enabled"] or row["kind"] != "tool-pack":
                continue
            if row["id"] in pids:
                continue
            pids.append(row["id"])
    for plugin_id in pids:
        try:
            _sync_toolpack(plugin_id)
            if plugin_id in _active_toolpacks:
                ok += 1
        except Exception:  # noqa: BLE001 启动期不因单个插件失败而中断
            continue
    return ok


# ── 操作（含审计与撤销） ────────────────────────────────

def _record_op(conn, user_id: str, op: str, plugin_id: str, before: dict | None) -> None:
    """在调用方给定的事务连接上写审计（不再自开连接，才能跟着外层事务一起回滚）。"""
    conn.execute(
        "INSERT INTO market_ops (id, user_id, op, plugin_id, before_json, undone, created_at) "
        "VALUES (?, ?, ?, ?, ?, 0, ?)",
        (db.new_id("op"), user_id, op, plugin_id, json.dumps(before, ensure_ascii=False) if before else None, db.now_iso()),
    )


def _journal_begin(user_id: str, op: str, plugin_id: str) -> int:
    """破坏性步骤之前落意图（dsh-market PendingRowJournal.begin）。

    只有 install/uninstall 需要——它们有进程级副作用（工具注册），
    DB 提交与注册同步之间被杀就会留半截状态。
    """
    def _write(conn):
        cur = conn.execute(
            "INSERT INTO market_journal (user_id, op, plugin_id, state, created_at) "
            "VALUES (?, ?, ?, 'begin', ?)",
            (user_id, op, plugin_id, db.now_iso()),
        )
        return int(cur.lastrowid or 0)

    return db.atomic(_write)


def _journal_finish(journal_id: int) -> None:
    db.execute(
        "UPDATE market_journal SET state = 'done', finished_at = ? WHERE id = ?",
        (db.now_iso(), journal_id),
    )


def sweep_market_journal() -> dict:
    """启动对账：把未 finish 的意图按 DB 真相收敛，并清扫幽灵注册。

    对应 dsh-market 的 repairOrphanedRows + 反向清扫。DA-AI 原先的
    `boot_installed()` 只做正向恢复（DB 有的注册回来），从不反向清扫——
    崩溃后 `_active_toolpacks` 里会留着 DB 已无记录的幽灵工具，且无人收敛。
    """
    fixed = {"install_dropped": 0, "uninstall_dropped": 0, "tools_cleared": 0}
    rows = db.rows("SELECT * FROM market_journal WHERE state = 'begin' ORDER BY id")
    for row in rows:
        uid, op, pid = row["user_id"], row["op"], row["plugin_id"]
        installed = db.one(
            "SELECT kind FROM market_plugins WHERE user_id = ? AND id = ?", (uid, pid)
        )
        if op == "install" and not installed:
            # 装到一半被杀：DB 没落行 = 当没装过（注册表下面统一清）
            fixed["install_dropped"] += 1
        elif op == "uninstall" and installed:
            db.execute(
                "DELETE FROM market_plugins WHERE user_id = ? AND id = ?", (uid, pid)
            )
            fixed["uninstall_dropped"] += 1
        db.execute(
            "UPDATE market_journal SET state = 'done', finished_at = ? WHERE id = ?",
            (db.now_iso(), row["id"]),
        )
    # 注册表对账：DB 里已无 enabled 安装记录的包，进程里就不该还活着
    for pid in list(_active_toolpacks):
        still = db.one(
            "SELECT 1 FROM market_plugins WHERE id = ? AND kind = 'tool-pack' "
            "AND enabled = 1 LIMIT 1",
            (pid,),
        )
        if not still:
            ctx = _active_toolpacks.pop(pid, None)
            if ctx is not None:
                ctx.dispose()
            fixed["tools_cleared"] += 1
    if any(fixed.values()):
        print(f"[market] 启动对账收敛了 {fixed}", flush=True)
    return fixed


def install(user_id: str, plugin_id: str) -> dict:
    entry = catalog_index().get(plugin_id)
    if not entry:
        # 拒装不在目录里的（dsh-market 同款守卫）
        raise MarketError("该插件不在市场目录中，拒装", 404)
    if get_installed(user_id, plugin_id):
        raise MarketError("已安装过该插件", 409)
    if entry["kind"] == "dsh-ref":
        # DSH 社区条目是 Cordis 宿主插件（跑在 DeepSeek Harness 运行时里），
        # 本站只提供浏览与跳转，装了也没有能执行它的宿主。
        raise MarketError("该条目来自 DSH 社区目录，需 DSH 宿主运行时，不可在本站安装", 400)
    payload = entry_payload(entry)
    manifest = {**entry, "payload": payload}
    now = db.now_iso()
    manifest_json = json.dumps(manifest, ensure_ascii=False)

    journal_id = _journal_begin(user_id, "install", plugin_id)

    def _write(conn) -> None:
        conn.execute(
            "INSERT INTO market_plugins (id, user_id, kind, name, version, manifest_json, enabled, installed_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (plugin_id, user_id, entry["kind"], entry["name"], entry["version"], manifest_json, now, now),
        )
        if entry["kind"] == "theme":
            # 主题互斥（dsh themes.activateTheme 语义）：装的主题即启用，其他主题下线
            conn.execute(
                "UPDATE market_plugins SET enabled = 0, updated_at = ? "
                "WHERE user_id = ? AND kind = 'theme' AND id != ?",
                (now, user_id, plugin_id),
            )
        _record_op(conn, user_id, "install", plugin_id, None)

    try:
        # 一条事务落三处：安装行 + 主题互斥 + 审计。原先三条裸 execute，
        # 中途失败会停在「互斥了一半」的状态。
        db.atomic(_write)
        if entry["kind"] == "tool-pack":
            _sync_toolpack(plugin_id)
    except Exception:
        # 装不上就把刚写的行删掉（含 _sync_toolpack 抛的空包错误），
        # 并清掉可能已注册的半个包。安装行已随事务回滚，审计是同一事务里
        # 写的也一起没了；这里再兜一次 delete，防的是「事务已提交、
        # _sync_toolpack 才失败」那条路留下的 install 审计——
        # 留着它，undo_last 会尝试恢复一个从未成功安装的插件。
        db.execute("DELETE FROM market_plugins WHERE user_id = ? AND id = ?", (user_id, plugin_id))
        db.execute(
            "DELETE FROM market_ops WHERE user_id = ? AND plugin_id = ? AND op = 'install' AND undone = 0",
            (user_id, plugin_id),
        )
        _sync_toolpack(plugin_id)  # 表里已无 enabled 行 → 摘掉注册
        raise
    _journal_finish(journal_id)
    return get_installed(user_id, plugin_id)  # type: ignore[return-value]


def uninstall(user_id: str, plugin_id: str) -> None:
    row = get_installed(user_id, plugin_id)
    if not row:
        raise MarketError("该插件未安装", 404)
    journal_id = _journal_begin(user_id, "uninstall", plugin_id)

    def _write(conn) -> None:
        conn.execute("DELETE FROM market_plugins WHERE user_id = ? AND id = ?", (user_id, plugin_id))
        _record_op(conn, user_id, "uninstall", plugin_id, row)

    db.atomic(_write)
    if row["kind"] == "tool-pack":
        _sync_toolpack(plugin_id)
    _journal_finish(journal_id)


def _apply_theme_enabled(conn, user_id: str, plugin_id: str, enabled: bool) -> None:
    """主题互斥 + 本行开关，全部在调用方的连接上执行（供事务内调用）。"""
    now = db.now_iso()
    if enabled:
        conn.execute(
            "UPDATE market_plugins SET enabled = 0, updated_at = ? WHERE user_id = ? AND kind = 'theme' AND id != ?",
            (now, user_id, plugin_id),
        )
    conn.execute(
        "UPDATE market_plugins SET enabled = ?, updated_at = ? WHERE user_id = ? AND id = ?",
        (1 if enabled else 0, now, user_id, plugin_id),
    )


def _set_theme_enabled(user_id: str, plugin_id: str, enabled: bool, record: bool = True) -> dict:
    """主题互斥：启用一个主题前先停用其他主题（dsh themes.activateTheme 语义）。"""
    before: dict[str, int] = {}
    for row in installed_rows(user_id):
        if row["kind"] == "theme":
            before[row["id"]] = int(row["enabled"])

    def _write(conn) -> None:
        _apply_theme_enabled(conn, user_id, plugin_id, enabled)
        if record:
            _record_op(conn, user_id, "enable" if enabled else "disable", plugin_id, before)

    db.atomic(_write)
    return before


def set_enabled(user_id: str, plugin_id: str, enabled: bool) -> dict:
    row = get_installed(user_id, plugin_id)
    if not row:
        raise MarketError("该插件未安装", 404)
    if row["kind"] == "theme":
        _set_theme_enabled(user_id, plugin_id, enabled)
        return get_installed(user_id, plugin_id)  # type: ignore[return-value]
    before = {"enabled": int(row["enabled"])}

    def _write(conn) -> None:
        conn.execute(
            "UPDATE market_plugins SET enabled = ?, updated_at = ? WHERE user_id = ? AND id = ?",
            (1 if enabled else 0, db.now_iso(), user_id, plugin_id),
        )
        _record_op(conn, user_id, "enable" if enabled else "disable", plugin_id, before)

    db.atomic(_write)
    if row["kind"] == "tool-pack":
        # 先落库再同步：_sync_toolpack 读的就是这张表
        _sync_toolpack(plugin_id)
    return get_installed(user_id, plugin_id)  # type: ignore[return-value]


def undo_last(user_id: str) -> str:
    """撤销最近一步未撤销的市场操作（安装/卸载/启停）。"""
    row = db.one(
        "SELECT id, op, plugin_id, before_json FROM market_ops "
        "WHERE user_id = ? AND undone = 0 ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (user_id,),
    )
    if not row:
        raise MarketError("没有可撤销的操作", 404)
    before = json.loads(row["before_json"]) if row["before_json"] else None
    op, plugin_id = row["op"], row["plugin_id"]
    # 进程级同步（工具包注册）放在事务提交之后统一做：_sync_toolpack 要读
    # 刚写完的表，放事务内会读到旧快照。
    sync_after: list[str] = []

    def _write(conn) -> str:
        if op == "install":
            cur = get_installed(user_id, plugin_id)
            if cur:
                conn.execute(
                    "DELETE FROM market_plugins WHERE user_id = ? AND id = ?", (user_id, plugin_id)
                )
                if cur["kind"] == "tool-pack":
                    sync_after.append(plugin_id)
            return f"已撤销安装「{plugin_id}」"
        if op == "uninstall":
            if before and not get_installed(user_id, plugin_id):
                manifest = json.loads(before["manifest_json"])
                # 恢复前重新过一遍目录校验：目录后来删掉/改坏该条目时，
                # 直接写回 manifest 会造出一行非法数据（2026-10-25 修）。
                entry = catalog_index().get(plugin_id)
                if entry is None:
                    raise MarketError(
                        f"目录里已没有「{plugin_id}」，无法恢复（请重新安装）", 409
                    )
                conn.execute(
                    "INSERT INTO market_plugins (id, user_id, kind, name, version, manifest_json, enabled, installed_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        before["id"], user_id, before["kind"], before["name"], before["version"],
                        before["manifest_json"], before["enabled"], before["installed_at"], db.now_iso(),
                    ),
                )
                if before["kind"] == "tool-pack":
                    sync_after.append(plugin_id)
            return f"已恢复「{plugin_id}」"
        # enable / disable：before 是主题整组快照或单行 enabled
        if isinstance(before, dict) and all(isinstance(v, int) for v in before.values()) and "enabled" not in before:
            for pid, enabled in before.items():
                conn.execute(
                    "UPDATE market_plugins SET enabled = ?, updated_at = ? WHERE user_id = ? AND id = ?",
                    (enabled, db.now_iso(), user_id, pid),
                )
                sync_after.append(pid)
        elif before and "enabled" in before:
            conn.execute(
                "UPDATE market_plugins SET enabled = ?, updated_at = ? WHERE user_id = ? AND id = ?",
                (before["enabled"], db.now_iso(), user_id, plugin_id),
            )
            sync_after.append(plugin_id)
        return f"已撤销对「{plugin_id}」的开关操作"

    msg = db.atomic(_write)
    db.execute("UPDATE market_ops SET undone = 1 WHERE id = ?", (row["id"],))
    for pid in sync_after:
        _sync_toolpack(pid)
    return msg


def market_tool_names() -> set[str]:
    """目录里所有工具包声明过的工具名（不论装没装）。

    executor 用它区分「市场工具」和「别处注册的插件工具」——前者要按用户门禁，
    后者（如内置的 current_time）不需要。
    """
    out: set[str] = set()
    for entry in load_catalog().get("plugins", []):
        if entry.get("kind") != "tool-pack":
            continue
        for tool in (entry.get("payload") or {}).get("tools", []):
            if tool.get("name"):
                out.add(tool["name"])
    return out


def active_tool_names(user_id: str) -> set[str]:
    """这个用户当前启用的市场工具名。

    工具是进程级注册的（全局一份），但**目录与执行都按用户过滤**——
    不然「我停用了，工具却还在（因为别的用户开着）」会表现成停用无效。
    """
    out: set[str] = set()
    for row in installed_rows(user_id):
        if row["kind"] != "tool-pack" or not row["enabled"]:
            continue
        entry = catalog_index().get(row["id"])
        if not entry:
            continue
        for tool in (entry.get("payload") or {}).get("tools", []):
            if tool.get("name"):
                out.add(tool["name"])
    return out


def market_tools_snapshot() -> list[dict]:
    """已激活的市场工具（供 /api/plugins 展示）。

    工具是进程级共享的，所以这里不再带 user_id——带上会误导成「只对这个人生效」。
    """
    out: list[dict] = []
    for pid, ctx in _active_toolpacks.items():
        out.append({"id": f"market.{pid}", "tools": list(ctx.tools)})
    return out
