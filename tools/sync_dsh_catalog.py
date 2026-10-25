"""DSH 社区目录同步：awesome-dsh-plugin.com → catalog.json 的 dsh-ref 条目。

蓝本 dsh-market 的官方目录（Cordis 宿主插件），本站只做「浏览+跳转」不可安装。

跑法：
    python tools/sync_dsh_catalog.py                        # 离线模式，读 work/dsh_plugins.json
    python tools/sync_dsh_catalog.py --url https://awesome-dsh-plugin.com/plugins.json
    python tools/sync_dsh_catalog.py --top 200 --input work/dsh_plugins.json

规则：
  - 热度 = downloads + 10 * stars，降序取 Top N；
  - kind 固定 dsh-ref，无 payload，install() 一律 400；
  - 幂等：先删 id 以 "dsh." 开头的旧条目再追加，count 重算；
  - 写回前备份 catalog.json.bak；
  - 尾部清缓存跑 market.load_catalog() 自校验。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CATALOG = ROOT / "app" / "data" / "market" / "catalog.json"
DEFAULT_INPUT = ROOT / "work" / "dsh_plugins.json"
DEFAULT_URL = "https://awesome-dsh-plugin.com/plugins.json"

# DSH 分类 → 中文标签（未覆盖的回退原值）
CATEGORY_ZH = {
    "agi": "智能体", "ui": "界面", "usage": "用量", "theme": "主题",
    "model": "模型", "identity": "身份", "session": "会话", "memory": "记忆",
    "tools": "工具", "wsl": "WSL", "browser": "浏览器", "vision": "视觉",
    "voice": "语音", "docs": "文档", "skill": "技能", "workflow": "工作流",
    "git": "Git", "notify": "通知", "dev": "开发", "security": "安全",
    "remote": "远程", "market": "市场", "fun": "趣味",
}


def fetch(url: str) -> dict:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "da-ai-market-sync/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def heat(p: dict) -> int:
    return (p.get("downloads") or 0) + 10 * (p.get("stars") or 0)


def to_entry(p: dict) -> dict:
    """DSH 目录条目 → 本站 dsh-ref 条目。install 命令不采（不可装）。"""
    desc = p.get("description") or {}
    entry = {
        "id": "dsh." + (p.get("name") or "").strip(),
        "name": p.get("name") or "",
        "owner": p.get("owner") or "",
        "kind": "dsh-ref",
        "category": p.get("category") or "dev",
        "version": p.get("version") or "0.0.0",
        "description": {
            "zh": (desc.get("zh") or desc.get("en") or "").strip(),
            "en": (desc.get("en") or "").strip(),
        },
        "url": p.get("url") or "",
        "added": p.get("added") or "",
        "capabilities": p.get("capabilities"),       # None=未扫描，[]=未检出
        "capabilityRedLines": p.get("capabilityRedLines") or [],
        "capabilityCheckedAt": p.get("capabilityCheckedAt"),
        "payload": {},
    }
    if p.get("npm"):
        entry["npm"] = p["npm"]
    if p.get("stars") is not None:
        entry["stars"] = p["stars"]
    if p.get("downloads") is not None:
        entry["downloads"] = p["downloads"]
        entry["downloadsStart"] = p.get("downloadsStart")
        entry["downloadsEnd"] = p.get("downloadsEnd")
    # 空值不留键，保持目录干净
    return {k: v for k, v in entry.items() if v not in (None, "", [], {}) or k in ("payload", "capabilities", "capabilityRedLines", "description", "url")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(DEFAULT_INPUT))
    ap.add_argument("--url", default="")
    ap.add_argument("--top", type=int, default=200)
    args = ap.parse_args()

    if args.url:
        print(f"拉取 {args.url} ...")
        data = fetch(args.url)
    else:
        src = Path(args.input)
        if not src.exists():
            print(f"离线快照不存在：{src}（先用 --url 拉一次）")
            return 2
        data = json.loads(src.read_text(encoding="utf-8"))

    plugins = data.get("plugins") or []
    print(f"源目录：{len(plugins)} 条（updated {data.get('updated')}）")

    ranked = sorted(plugins, key=heat, reverse=True)
    picked = [p for p in ranked[: args.top] if (p.get("name") or "").strip()]
    print(f"按热度取 Top {len(picked)}（最小热度 {min(heat(p) for p in picked) if picked else 0}）")

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    # 幂等：删旧 dsh.* 条目
    before = len(catalog.get("plugins") or [])
    kept = [e for e in catalog.get("plugins") or [] if not str(e.get("id", "")).startswith("dsh.")]
    removed = before - len(kept)

    # dsh 分类标签：只收 Top N 实际覆盖的分类
    used_cats = sorted({e["category"] for e in (to_entry(p) for p in picked)})
    cats = dict(catalog.get("categories") or {})
    for cid in used_cats:
        cats[f"dsh.{cid}"] = {"zh": f"DSH·{CATEGORY_ZH.get(cid, cid)}", "en": f"DSH {cid}"}

    entries = []
    seen_ids: set[str] = set()
    for p in picked:
        e = to_entry(p)
        if e["id"] in seen_ids:
            # 不同 owner 的同名插件：用 owner 消歧（保序：热度高的先占裸名）
            e["id"] = f"dsh.{e['owner']}/{e['name']}"
        if e["id"] in seen_ids:  # 同 owner 同名（异常数据）→ 跳过
            continue
        seen_ids.add(e["id"])
        entries.append(e)
    catalog["plugins"] = kept + entries
    catalog["categories"] = cats
    catalog["count"] = len(catalog["plugins"])
    import datetime
    catalog["updated"] = datetime.date.today().isoformat()

    # 备份 + 写回
    shutil.copy2(CATALOG, CATALOG.with_suffix(".json.bak"))
    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"写入 {CATALOG.name}：+{len(entries)} 条 dsh-ref（移除旧 {removed} 条），count={catalog['count']}")

    # 自校验
    from app.services import market
    market._catalog_cache = None
    loaded = market.load_catalog()
    kinds: dict[str, int] = {}
    for e in loaded["plugins"]:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print("load_catalog 自校验 OK：", kinds)
    print("建议再跑：python tools/validate_catalog.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
