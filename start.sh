#!/usr/bin/env bash
# 启动脚本：加载 .env → exec uvicorn。
# 平台不会展开 startCmd 里的 $PORT，所以端口一律在脚本内读。
set -euo pipefail
cd "$(dirname "$0")"

if [ -f ./.env ]; then
  set -a
  . ./.env
  set +a
fi

export PORT="${PORT:-8080}"

# 数据目录：线上 /opt/da-data（跨发布保留），本地回落项目内 data/
# Windows 的 Git Bash 会把 /opt 改写成安装目录下的路径，本地开发直接用项目内 data/
if [ -z "${DATA_DIR:-}" ]; then
  case "$(uname -s 2>/dev/null || echo linux)" in
    MINGW*|MSYS*|CYGWIN*) DATA_DIR="$(pwd)/data" ;;
    *) DATA_DIR="/opt/da-data" ;;
  esac
fi
if ! mkdir -p "$DATA_DIR" 2>/dev/null || [ ! -w "$DATA_DIR" ]; then
  DATA_DIR="$(pwd)/data"
  mkdir -p "$DATA_DIR"
  echo "[start] 无法写入默认数据目录，回落到 $DATA_DIR"
fi
export DATA_DIR
mkdir -p "$DATA_DIR/files" "$DATA_DIR/extracted" "$DATA_DIR/tmp" "$DATA_DIR/backups"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  # 优先用项目自带的虚拟环境：系统 python 不一定装了这个项目的依赖
  for candidate in .venv/bin/python .venv/Scripts/python.exe; do
    if [ -x "$candidate" ]; then PY="$candidate"; break; fi
  done
fi
if [ -z "$PY" ]; then
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
  done
fi
[ -n "$PY" ] || { echo "[start] 找不到 python"; exit 1; }

echo "[start] port=${PORT} data=${DATA_DIR} key=$([ -n "${MINIMAX_API_KEY:-}" ] && echo set || echo MISSING)"

exec "$PY" -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips='*' \
  --log-level info
