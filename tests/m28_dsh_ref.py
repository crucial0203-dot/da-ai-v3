"""M28 验收：dsh-ref 条目（DSH 社区目录浏览型插件）。

跑法（需 8020 已起）：
    python tests/m28_dsh_ref.py

覆盖：
  D1 目录里 dsh-ref 共 200 条，全部带合法 github/npm 来源
  D2 dsh-ref 校验：缺 url / 内网 url / capabilities 非数组 → MarketError
  D3 install dsh.* → 400「需 DSH 宿主」
  D4 dsh 条目不出现在工具目录（/api/plugins）
  D5 undo 不受影响（对可装插件照常工作）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import (  # noqa: E402
    Client, admin_login, check, join, make_invite, summary,
)


def env(k: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(k + "="):
            return line.split("=", 1)[1].strip()
    return ""


def main() -> int:
    # D1 目录校验（不起服务，直接读目录）
    from app.services import market
    market._catalog_cache = None
    catalog = market.load_catalog()
    dsh = [e for e in catalog["plugins"] if e["kind"] == "dsh-ref"]
    check("D1 dsh-ref 共 200 条", len(dsh) == 200, str(len(dsh)))
    check("D1 全部 url 指向 github/npm 等公网",
          all(e.get("url", "").startswith("https://") for e in dsh),
          str([e.get("url", "")[:40] for e in dsh if not e.get("url", "").startswith("https://")][:3]))
    check("D1 全部带中文描述", all(e["description"].get("zh") for e in dsh))
    check("D1 id 唯一", len({e["id"] for e in dsh}) == len(dsh))

    # D2 校验分支
    def expect_err(name: str, entry: dict, frag: str) -> None:
        try:
            market._validate_entry(entry)
            check(name, False, "未抛错")
        except market.MarketError as exc:
            check(name, frag in str(exc), str(exc))

    base = dict(dsh[0])
    expect_err("D2 缺 url 拒", {**base, "url": ""}, "url")
    expect_err("D2 ftp url 拒", {**base, "url": "ftp://example.com/x"}, "url")
    expect_err("D2 内网 url 拒", {**base, "url": "http://127.0.0.1/x"}, "内网")
    expect_err("D2 capabilities 非数组拒", {**base, "capabilities": "shell"}, "capabilities")
    check("D2 合法条目过校验", (market._validate_entry(dict(base)) or None) is None)

    # D3/D4/D5 走服务
    admin = admin_login(env("ADMIN_PASSWORD"))
    code = make_invite(admin, count=1, quota=30, note="m28")
    a = Client()
    join(a, code, "m28甲")

    status, data = a.post("/api/market/install", {"id": dsh[0]["id"]})
    check("D3 install dsh.* → 400", status == 400, f"{status} {data}")
    check("D3 报错文案含「DSH 宿主」", "DSH 宿主" in str(data), str(data))

    status, data = a.get("/api/plugins")
    names = {t.get("name") for t in (data.get("tools") or [])} if isinstance(data, dict) else set()
    dsh_tool_leak = [n for n in names if n and n.startswith("dsh_")]
    check("D4 dsh 条目不进工具目录", not dsh_tool_leak, str(dsh_tool_leak[:5]))

    status, data = a.get("/api/market/catalog")
    entry = next(e for e in data["plugins"] if e["kind"] == "dsh-ref")
    check("D4 catalog 带 no_install 标记", entry.get("no_install") is True, str(entry.get("no_install")))
    check("D4 catalog 条目透传 url/downloads",
          bool(entry.get("url")) and "downloads" in entry, str({k: entry.get(k) for k in ("url", "downloads")}))

    # D5 undo：装一个可装的工具包再撤销，确认 dsh 逻辑没有破坏 undo 链
    status, data = a.post("/api/market/install", {"id": "tool.calc"})
    check("D5 装 tool.calc 成功", status == 200, f"{status} {data}")
    status, data = a.post("/api/market/undo", {})
    check("D5 undo 成功", status == 200, f"{status} {data}")
    status, data = a.get("/api/market/catalog")
    calc = next(e for e in data["plugins"] if e["id"] == "tool.calc")
    check("D5 undo 后 tool.calc 已卸载", calc.get("installed") is False, str(calc.get("installed")))

    return summary()


if __name__ == "__main__":
    raise SystemExit(main())
