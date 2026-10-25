"""单进程启动入口，不依赖 shell。

为什么不用 start.sh：发布平台的 startCmd 不保证经过 shell，`$PORT` 也不会被展开；
实测 `bash install.sh` 这一层以 exit 127 失败（找不到命令），而同一份 pip 安装
单独跑是好的。所以入口改成 Python：端口自己从环境变量读，.env 自己加载。

    python3 run.py

本地等价于 ./start.sh，两者共用 app/config.py 的默认值。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def load_env() -> None:
    """把项目根的 .env 读进环境变量，已存在的键不覆盖（平台注入的优先）。"""
    path = BASE / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def resolve_data_dir() -> str:
    """线上用 /opt/da-data（跨发布保留），写不进去就回落到项目内 data/。"""
    explicit = os.environ.get("DATA_DIR", "").strip()
    if explicit:
        chosen = Path(explicit)
    elif os.name == "nt":
        chosen = BASE / "data"
    else:
        chosen = Path("/opt/da-data")
    try:
        for sub in ("", "files", "extracted", "tmp", "backups"):
            (chosen / sub).mkdir(parents=True, exist_ok=True)
        probe = chosen / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        chosen = BASE / "data"
        for sub in ("", "files", "extracted", "tmp", "backups"):
            (chosen / sub).mkdir(parents=True, exist_ok=True)
        print(f"[run] 数据目录不可写，回落到 {chosen}", flush=True)
    os.environ["DATA_DIR"] = str(chosen)
    return str(chosen)


def main() -> int:
    load_env()
    data_dir = resolve_data_dir()
    port = int(os.environ.get("PORT") or 8080)
    print(f"[run] port={port} data={data_dir} "
          f"key={'set' if os.environ.get('MINIMAX_API_KEY') else 'MISSING'}", flush=True)

    import uvicorn  # 放在环境和数据目录就绪之后再导入

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
