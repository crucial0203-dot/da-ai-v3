#!/usr/bin/env bash
# 安装期把依赖装完并建好数据目录；start 阶段只做 import，保证 60 秒就绪窗口内起得来。
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
echo "[install] python: $($PY -V 2>&1)"

$PY -m pip install --no-cache-dir --upgrade pip >/dev/null 2>&1 || true
$PY -m pip install --no-cache-dir -r requirements.txt

$PY - <<'EOF'
import fastapi, uvicorn, httpx, docx, pptx, pypdf, PIL
print("[install] deps ok fastapi", fastapi.__version__)
EOF

if [ -f ./.env ]; then
  set -a; . ./.env; set +a
fi

DATA_DIR="${DATA_DIR:-/opt/da-data}"
if mkdir -p "$DATA_DIR/files" "$DATA_DIR/extracted" "$DATA_DIR/tmp" "$DATA_DIR/backups" 2>/dev/null; then
  chmod 700 "$DATA_DIR" 2>/dev/null || true
  echo "[install] data dir ready: $DATA_DIR"
else
  echo "[install] WARN 无法创建 $DATA_DIR，运行时将回落到项目内 data/"
fi

[ -n "${MINIMAX_API_KEY:-}" ] && echo "[install] model key: set" || echo "[install] WARN model key 未配置"
