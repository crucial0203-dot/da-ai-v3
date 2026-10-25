# 发布检查单（DA的AI V4.0）

发布 = 把 `dist/` 整包上传到沙箱并重启。沙箱运行时无 node、无构建、无外网装依赖，
所以**一切产物必须在本地准备好**。这份清单就是 `tools/pack.py` 断言的散文版：
pack 能机器判的这里不再重复理由，只讲人要做什么、为什么。

## 1. 构建前端

```bash
cd web && npm ci && npm run build
```

产物落 `app/static/ui/`（index.html + bundle/）。`npm run build` 内置
`check-bundle`：js+css 合计 >2.2MB 或出现 .map 直接失败。FastAPI 用 StaticFiles
从磁盘读这份产物，所以**前端改完 build 即生效，不用重启服务**。

## 2. 打包

```bash
python tools/pack.py
```

必须输出 `[pack] OK`。它断言：白名单文件齐全、无 .db / __pycache__ / tests、
static/ui 存在且在预算内、总包 ≤3.0MB。任何一条不过都不允许发布。

## 3. 核对 dist/ 内容

`install.sh / start.sh / run.py / requirements.txt / .env / app/`（app 含 static/ui）。
`.env` 是沙箱里唯一的环境注入通道——它缺失或内容不对，服务起不来也无从调试。

## 4. 核对 .env

- 必须有：`MINIMAX_API_KEY`、`ADMIN_PASSWORD`。
- 必须没有：`DATA_DIR`（生产走默认 /opt/da-data）、`DEBUG_MODE`、`FAKE_UPSTREAM`、
  `SEARCH_ALLOW_LOOPBACK`、`WEB_SEARCH_FAKE`（后三个是测试专用，pack 会拦截）。
- 副本与源字节数一致（pack 已断言）。

## 5. 测试全绿

- 后端：`m2`–`m21` 全绿（含 m21_migration：V3.x 老库冷启动迁移 + 回填）。
- 前端：`p0`–`p6` 全绿（Playwright，打 data-testid）。
- 服务没改前端时至少跑 `p1_smoke`；动了 chat/任务链路必跑 `m3 m12 p2`。

## 6. 数据兼容

线上库在 `/opt/da-data/app.db`，**跨发布保留**。迁移原则：
- 只加表、加可空列（SQLite ALTER TABLE ADD COLUMN 是元数据操作，毫秒级）；
- 启动时自动迁移（`db._migrate`），迁移前先备份到 `backups/`；
- 失败不阻断启动：读路径 .get() 容错、写路径 has_column() 降级。
**禁止**改列语义、删列、改枚举值含义——线上有真实数据，没有回滚窗口。
发布前手工快照：`cp /opt/da-data/app.db /opt/da-data/backups/pre-vX.Y.db`。

## 7. 上线冒烟

1. `/healthz` 返回 Pod alive（平台探针）。
2. `/__health` 看 fts5 / journal_mode / migrations。
3. `/__net` 看联网三档状态。
4. 登录 → 发一条对话 → SSE 正常流到 done。
5. PPT 任务：生成 → 待确认 → 确认 → 交付（六节点链路完整走一遍）。

## 回滚

- 代码回滚 = 重发上一版 `dist/`（git 里都有）。
- 数据回滚 = 用 `backups/` 里的快照覆盖 `app.db`（迁移是加法，覆盖回去即可）。
- 不要做就地 patch：线上改的每一行都要回到仓库，否则下一版发布会把它冲掉。

## 环境事实（踩过的坑）

- `$PORT` 不在 startCmd 里展开 → `startCmd: bash start.sh`。
- installCmd 别用 shell 脚本（沙箱 exit 127）→ `python3 -m pip install -r requirements.txt`。
- `/healthz` 被平台占用（返回 Pod alive）→ 应用健康接口用 `__health`。
- 运行沙箱无外网：启动时联网拉模型必卡死（HF 不可达）；MiniMax api 可达。
- 公网有腾讯云 WAF：带 Authorization 头的 POST 可能被拦，管理操作走沙箱内部 127.0.0.1。
- 改 `app/` 下 Python 必须重启进程（run.py 无 reload）；前端只 build 不重启。
- **上传器把名为 `dist` 的目录当构建产物剔出包**（2026-10-23 实测：app/static/dist/
  整个消失 → `/` 500 → 60s 探活失败 SITES_START_FAILED）。前端产物目录因此叫
  `app/static/ui/`（子目录 `bundle/`），**任何随包发布的目录都不要叫 dist/build/out**。
- **.sh / .env 必须 LF 换行**：CRLF 的 `set -euo pipefail\r` 在 Linux bash 直接
  'invalid option name' 起不来，.env 带 \r 会把值污染成 `xxx\r`。pack.py 已做
  归一化 + 硬断言，源文件也别用 Windows 编辑器存成 CRLF。
- 发布失败 ≠ 线上没动：失败的更新也会重启沙箱，原链接当场打不开，要立刻修复重发。
