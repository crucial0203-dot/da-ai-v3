"""打包出可发布目录 dist/。

发布目录只能是项目根：start.sh 里是 `uvicorn app.main:app`，服务根必须是含
app/ 包的那一层。但根目录还有 .venv（几十兆）、data/（本地测试库与上传文件）、
tests/、__pycache__，整目录上传会把这些一起送进沙箱。这里按白名单复制一份干净副本。

用法：python tools/pack.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

FILES = ["install.sh", "start.sh", "run.py", "requirements.txt", ".env"]
TREE = ["app"]
SKIP_DIRS = {"__pycache__", "var", "tmp", ".pytest_cache", "extracted", "files", "backups"}
SKIP_SUFFIX = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".log"}
FORBIDDEN = ["data", "tests", "docs", "dist", ".venv", "node_modules", "tools"]
SIZE_LIMIT_MB = 3.0
# 前端产物预算（与 web/scripts/check-bundle.mjs 同值，双闸）
BUNDLE_LIMIT_MB = 2.2


def _ignore(_dir: str, names: list[str]) -> set[str]:
    drop = set()
    for name in names:
        if name in SKIP_DIRS or Path(name).suffix in SKIP_SUFFIX:
            drop.add(name)
    return drop


def _env_pairs(text: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        pairs[key.strip()] = value.strip()
    return pairs


def main() -> int:
    if not (ROOT / ".env").exists():
        print("[pack] 缺少 .env，先复制 .env.example 并填值")
        return 1

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    for name in FILES:
        src = ROOT / name
        if not src.exists():
            print(f"[pack] 缺少 {name}")
            return 1
        shutil.copy2(src, DIST / name)

    for name in TREE:
        shutil.copytree(ROOT / name, DIST / name, ignore=_ignore)

    # ---- 换行符归一化：bash 在 Linux 上不认 CRLF（start.sh 吃到 \r 直接
    # 'invalid option name' 起不来），.env 带 \r 会把值污染成 'xxx\r'。
    # 只归一 shell 脚本与 .env，其余文本（py/html/js）CRLF 无害不动。----
    lf_targets = [DIST / name for name in FILES] + list(DIST.rglob("*.sh"))
    for p in lf_targets:
        data = p.read_bytes()
        if b"\r" in data:
            p.write_bytes(data.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))

    # ---- 断言：任何一条不过都不允许发布 ----
    problems: list[str] = []
    notes: list[str] = []

    for forbidden in FORBIDDEN:
        if (DIST / forbidden).exists():
            problems.append(f"dist/{forbidden} 存在：不该上传")
    if (DIST / "app" / "var").exists():
        problems.append("dist/app/var 存在：本地测试库会被当成生产库传上去")
    if not (DIST / ".env").exists():
        problems.append("dist/.env 缺失：沙箱里没有别的注入通道")
    if not (DIST / "app" / "main.py").exists():
        problems.append("dist/app/main.py 缺失")
    if list((DIST / "app").rglob("*.db")):
        problems.append("dist/app 下还有 .db 文件")
    if list((DIST / "app").rglob("__pycache__")):
        problems.append("dist/app 下还有 __pycache__")

    # ---- 换行符硬断言：.sh/.env 混进 CRLF 一律不许发布 ----
    crlf_bad = [
        str(p.relative_to(DIST))
        for p in [DIST / name for name in FILES] + list(DIST.rglob("*.sh"))
        if b"\r" in p.read_bytes()
    ]
    if crlf_bad:
        problems.append(f"存在 CRLF 换行（bash 起不来）：{', '.join(crlf_bad)}")

    # ---- 前端产物断言：dist 必须随包发布（沙箱运行时不能构建）----
    # 目录名是 ui/ 不是 dist/：发布上传器把名为 dist 的目录当构建产物剔包。
    dist_html = DIST / "app" / "static" / "ui" / "index.html"
    dist_assets = DIST / "app" / "static" / "ui" / "bundle"
    if not dist_html.exists():
        problems.append("dist/app/static/ui/index.html 缺失：先在 web/ 执行 npm run build")
    elif not dist_assets.is_dir():
        problems.append("dist/app/static/ui/bundle 缺失：构建产物不完整")
    else:
        bundle = 0
        maps: list[str] = []
        for p in dist_assets.iterdir():
            if p.suffix == ".map":
                maps.append(p.name)
            if p.suffix in {".js", ".css"}:
                bundle += p.stat().st_size
        bundle_mb = bundle / 1024 / 1024
        if maps:
            problems.append(f"前端产物带 sourcemap：{', '.join(maps[:5])}")
        if bundle > BUNDLE_LIMIT_MB * 1024 * 1024:
            problems.append(
                f"前端产物 js+css {bundle_mb:.2f}MB 超 {BUNDLE_LIMIT_MB}MB 预算"
            )
        else:
            notes.append(f"前端产物 js+css {bundle_mb:.2f}MB / {BUNDLE_LIMIT_MB}MB")

    env_text = (DIST / ".env").read_text(encoding="utf-8", errors="ignore")
    env = _env_pairs(env_text)
    if env.get("DATA_DIR"):
        problems.append(f"dist/.env 显式设了 DATA_DIR={env['DATA_DIR'][:40]}：生产要走默认 /opt/da-data")
    if env.get("DEBUG_MODE", "").lower() in {"1", "true", "yes", "on"}:
        problems.append("dist/.env 开了 DEBUG_MODE：线上不该带调试开关")
    if env.get("FAKE_UPSTREAM", "").lower() in {"1", "true", "yes", "on"}:
        problems.append("dist/.env 开了 FAKE_UPSTREAM：假上游会顶掉真模型")
    if env.get("SEARCH_ALLOW_LOOPBACK", "").lower() in {"1", "true", "yes", "on"}:
        problems.append("dist/.env 开了 SEARCH_ALLOW_LOOPBACK：测试专用，线上禁止")
    if env.get("WEB_SEARCH_FAKE", "").lower() in {"1", "true", "yes", "on"}:
        problems.append("dist/.env 开了 WEB_SEARCH_FAKE：测试专用，线上禁止")
    if not env.get("MINIMAX_API_KEY"):
        problems.append("dist/.env 没有 MINIMAX_API_KEY")
    if not env.get("ADMIN_PASSWORD"):
        problems.append("dist/.env 没有 ADMIN_PASSWORD")
    if env.get("DAILY_CALL_QUOTA") and env["DAILY_CALL_QUOTA"] != "20":
        notes.append(f"每日额度被改成 {env['DAILY_CALL_QUOTA']} 次（默认 20）")

    # 源与副本的 .env 必须一致（只比字节数，不打印内容）
    src_len = (ROOT / ".env").stat().st_size
    dst_len = (DIST / ".env").stat().st_size
    if src_len != dst_len:
        problems.append(f".env 复制不完整：源 {src_len}B / 副本 {dst_len}B")

    total = sum(p.stat().st_size for p in DIST.rglob("*") if p.is_file())
    size_mb = total / 1024 / 1024
    files = sum(1 for p in DIST.rglob("*") if p.is_file())
    print(f"[pack] dist 文件数={files} 体积={size_mb:.2f}MB .env={dst_len}B")
    for note in notes:
        print(f"[pack] 注意：{note}")

    if size_mb > SIZE_LIMIT_MB:
        problems.append(f"dist 体积 {size_mb:.2f}MB 超过 {SIZE_LIMIT_MB}MB，说明带了不该带的东西")
    if problems:
        print("[pack] FAILED")
        for item in problems:
            print("  -", item)
        return 1

    print("[pack] OK 可以发布", DIST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
