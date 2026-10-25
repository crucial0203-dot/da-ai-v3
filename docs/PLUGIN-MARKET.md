# 插件市场 v0.2 —— 移植设计（蓝本：dsh-market / DSH 插件市场）

> 参考源码：`D:\study\workflow-ui-references\dsh-market`（DSH 内置插件市场，TypeScript）
> 与 `deepseek-harness` 的插件契约（Cordis 微内核 + React 槽位 + 可逆注册）。
> 本文是移植对照与取舍记录；实现见 `app/services/market.py`、`web/src/plugins/market.ts`、`web/src/views/Plugins.tsx`。

## 一、移植对照表

| dsh-market 机制（源码位置） | 我们的实现 | 取舍 |
|---|---|---|
| 社区目录 `plugins.json`：`{updated, count, categories, plugins[]}`（src/registry.ts:92） | `app/data/market/catalog.json` 打包内置快照，`GET /api/market/catalog` | 目录实时拉取 → 快照（部署沙箱无外网）；条目保留双语描述/星数/作者/能力披露字段 |
| 目录校验 validate-registry（scripts/validate-registry.mjs） | `tools/validate_catalog.py` + market.load_catalog() 入库校验 | 裁剪为：必填字段、id 唯一、kind 枚举、每类 payload 形状、中文描述必填 |
| 安装：只装 curated 目录里的，拒装未知源（routes.ts:6295） | `install()` 目录外 id → 404 拒装 | 语义照抄 |
| 安装链路：源优先级（npm→Release tarball→源码）+ 快照回滚 + 假成功守卫（routes.ts:6277、install.ts:775） | DB 事务写入 `market_plugins`（manifest_json=安装时点快照）；失败即删行回滚 | pnpm/node_modules 整条链不搬，改为 DB 注册表 |
| 热开关：`cordis.patch.yml` + HMR ~1s 重组（src/hot.ts、patch.ts） | `enabled` 列 + 前端可逆注册（slots.tsx 的 disposer）即时生效 | 机制换了，「停用即回滚、页面立刻变回去」的语义保留 |
| 主题换肤：`ThemeDef{colorScheme,tokens}` → CSS 变量、互斥激活（src/themes.ts:28-56） | `web/src/plugins/theme.ts`：token 覆写 `<html>` + `data-market-theme` 标记；启用主题互斥停用其他主题 | 直接搬，效果等价 |
| UI 面板：Discover / Themes / Favorites / Installed + 确认安装对话框（client.js:12839） | `/plugins` 市场页四 Tab + 搜索 + 分类 chips + 卡片 + 确认框 | 信息架构照抄；分组/备注/Blocked/诊断后置 |
| 收藏/分组/备注存 profile `state.json`（hot.ts:483） | 收藏 localStorage（`da_market_favs`）；安装状态 SQLite `market_plugins`（按用户隔离） | state.json → DB + localStorage |
| 操作队列 + 一步回滚（routes.ts:1603） | `market_ops` 审计表 + `POST /api/market/undo`（撤销最近一步） | 只做单步撤销 |
| 更新检查（src/updates.ts） | 目录 version 与已装 version 同屏可见（升级流程后置） | 裁剪 |
| 启停防呆（禁停自身、失败显式报 restart、开了不生效就拒绝） | 不在目录拒装；工具包启用失败即拒绝并回滚；主题互斥 | 语义对齐 |

## 二、三类可安装插件

| kind | payload | 生效方式 | 示例 |
|---|---|---|---|
| `theme` | `colorScheme` + `tokens`（CSS 变量集） | 前端即时换肤，互斥激活，停用恢复基础主题 | 深空蓝 / 暖纸 / 霓虹网格 |
| `ui-module` | `module`（`app/data/market/modules/*.js`） | 前端 Blob 动态 `import()` → `activate(ctx)` → 槽位注册（可逆） | 每日一言 / 写作快捷条 / 专注时钟 |
| `tool-pack` | `tools[]`（声明 + 通用 handler） | 后端注册进工具目录（可逆），停用即下架 | 计算器 / 文本统计 / 模板渲染 |

ui-module 协议与内置插件同构（`web/src/plugins/registry.ts`）：

```js
export default {
  manifest: { id, name, version },
  activate(ctx) {          // ctx = { slots.register, toast, React, h }
    ctx.slots.register("sidebar.footer", { id, component, order });
    return () => {};       // 可选 disposer
  },
};
```

## 三、环境约束带来的取舍

1. **服务端无外网** → 目录用打包快照（dsh 弃用的 snapshot 模式恰好适用）；更新目录 = 改 `app/data/market/catalog.json` 重新发布。
2. **不执行任意第三方代码于服务端** → tool-pack 是**声明式**工具（内置 4 种安全 handler：calc 白名单算式 / text_stats / template / echo），不是任意 Python。真·代码插件只存在于浏览器侧（ui-module）。
3. **CSP**：`script-src` 放行 `blob:`（动态模块加载所需）；模块只能从市场目录装（拒装未知源）。
4. **安装状态按用户隔离**（user_id）；tool-pack 启用后工具是进程级注册——已知简化，多用户互见工具。

## 四、明确不搬

pnpm 陷阱自愈、区域加速/GitHub 代理路由、WebDAV/Gist 备份、capabilities 静态扫描、Desktop 桥接与启动恢复面板、giscus 评论、多源 Index（15s 预算那套）、load-order 拖拽。
