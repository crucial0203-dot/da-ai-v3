"""插件市场目录校验（构建期跑一遍，防止目录写坏进包）。

用法：python tools/validate_catalog.py
校验规则是 dsh-market validate-registry 的裁剪版：必填字段、id 唯一、
kind 枚举、每类 payload 形状、中文描述必填。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import market  # noqa: E402


def main() -> int:
    try:
        catalog = market.load_catalog()
    except market.MarketError as exc:
        print(f"FAIL: {exc.message}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: 目录解析失败：{exc}")
        return 1

    plugins = catalog.get("plugins", [])
    kinds: dict[str, int] = {}
    for entry in plugins:
        kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
        if entry["kind"] == "ui-module":
            try:
                market.entry_payload(entry)
            except market.MarketError as exc:
                print(f"FAIL: {exc.message}")
                return 1
    print(f"OK: {len(plugins)} 个插件  {kinds}  updated={catalog.get('updated')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
