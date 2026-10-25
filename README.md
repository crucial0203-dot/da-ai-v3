# DA的AI V3

个人 AI 工作台：**流式对话 + 工具回路 + 工作流画布 + 插件市场 + 制品预览 + Office 生成**。
后端 FastAPI + SQLite（单进程，无外部依赖），前端 React + Vite + TypeScript，一键打包发布为公网应用。

线上：<https://da-ai.app.workbuddy.host/?sharecode=2GAE2PHAAJ>

---

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | FastAPI + uvicorn（单进程，asyncio SSE） |
| 存储 | SQLite（`data/app.db`，WAL；线上落在 `/opt/da-data`，跨发布保留） |
| 模型 | MiniMax（Anthropic 兼容端点，思考档位 `output_config.effort` 显式传 `medium`） |
| 前端 | React 19 + TypeScript + Vite + React Router + Zustand |
| 画布 | `@xyflow/react` v12 + `@dagrejs/dagre` 自动布局 |
| Office | python-docx / python-pptx（生成后服务端回读断言，9 项校验） |

## 目录

```
app/
  routers/     HTTP 层：auth/chat/tasks/workflows/market/tools/rag/memory/artifacts/generate/admin…
  services/    业务层：tools/(执行器/协议/围栏解析) workflow/(引擎/校验) llm/ rag/ 市场/制品/文档构建
  static/ui/   前端构建产物（vite outDir，目录名刻意不叫 dist——发布器会剔掉同名构建目录）
  data/market/ 插件市场目录数据（由 tools/sync_dsh_catalog.py 同步）
web/src/       前端源码：views/(画布/任务/市场/设置…) components/chat/(工具卡/审批卡/制品卡) lib/
tests/         验收套件：m*.py（接口/业务，httpx 直连）+ p*.js（浏览器，playwright-core）
tools/         pack.py（打包发布目录）、sync_dsh_catalog.py、validate_catalog.py
docs/          DEPLOY.md（发布链路）、PLUGIN-MARKET.md（市场机制）
```

## 本地跑起来

```bash
# 1) 后端依赖
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2) 配置（复制模板后填真实值；本文件不含任何真实密钥）
cp .env.example .env
#   MINIMAX_API_KEY / SECRET_KEY / ADMIN_PASSWORD 必填

# 3) 前端构建（产物写进 app/static/ui/）
cd web && npm install && npm run build && cd ..

# 4) 起服务（端口取自 $PORT，本地默认 8000）
.venv/bin/python run.py
```

Windows 侧：`.venv\Scripts\python.exe run.py`；`start.sh` 与 `.env` **必须是 LF**（CRLF 会让 Linux 上的 `set -euo pipefail\r` 直接起不来）。

## 测试

```bash
# 接口/业务套件（先起本地服务，默认 127.0.0.1:8020）
.venv/Scripts/python.exe tests/m3_chat.py          # 单个套件
DA_BASE=http://127.0.0.1:8020 .venv/Scripts/python.exe tests/m30_s1_batch.py

# 浏览器套件（需要 playwright-core）
NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p16_batch2.js
```

跑假上游（不消耗真实模型）：`DEBUG_MODE=1 FAKE_UPSTREAM=1`——**两个都得带**，缺 `DEBUG_MODE` 时假上游不接管。

## 发布

```bash
.venv/Scripts/python.exe tools/pack.py     # 白名单复制出 dist/，含体积与 CRLF 断言
```

`dist/` 即可发布目录：启动命令 `python3 run.py`（端口自己读 `$PORT`），安装命令 `python3 -m pip install -r requirements.txt`。
细节见 [docs/DEPLOY.md](docs/DEPLOY.md)。

## 安全约定（这些绝不入库，已在 .gitignore 拦）

- `.env`：真实密钥与管理员密码，只有 `.env.example` 可提交
- `data/`：SQLite 库、用户上传、抽取缓存、备份
- `docs/credentials.txt`：本机凭据
- `work/`、`.bak/`、`logs-*.log`：中间产物与备份
- `web/node_modules/`、`.venv/`、`__pycache__/`

另外：`.gitattributes` 把仓库内所有文本钉死 LF——发布链路的 `.sh`/`.env` 遇到 CRLF 会直接起不来。
