/** p18 · 工具结果「中性卡」浏览器验收（role='tool' 落库后的渲染）。
 *
 * 背景：早期版本把工具回注原本以 role='user' 落库，前端把它渲染成「你」的气泡，
 * 里面是一坨 <<<TOOL_RESULT ...>>> 裸标记。修复后后端改以 role='tool' 落库，
 * 前端新增 msg-tool 中性卡分支。本脚本验证这条链路端到端成立：
 *   1) 触发工具调用后，工具结果以 role='tool' 落库（直查库坐实）；
 *   2) 刷新读库 → 渲染成 [data-testid=msg-tool] 中性卡（标签「工具结果」）；
 *   3) 不再冒充用户气泡（[data-testid=msg-user][data-pure-tool] 应为 0）；
 *   4) 卡内显示具名工具、默认折叠、无 <<<TOOL_RESULT 裸标记泄漏。
 *
 * 运行：
 *   NODE_PATH=D:/workbuddyGZ/da-staff/node_modules \
 *   DA_BASE=http://127.0.0.1:8021 PY=/path/to/python.exe \
 *   node tests/p18_trae_tool_role_browser.js
 * 前置：服务已起并带 DEBUG_MODE=1 FAKE_UPSTREAM=1（脚本用 #fake 触发确定性工具调用，
 *       tool_name 取 rag_query —— 未绑知识库的任务必然回一条 error 结果，不需联网）。
 */
const { chromium } = require("playwright-core");
const fs = require("fs"), path = require("path");

const ROOT = path.resolve(__dirname, "..");
const BASE = process.env.DA_BASE || "http://127.0.0.1:8021";
const PY = process.env.PY || "python";

/** esbuild 用于把 TS 工具函数在 Node 侧求值；优先本地 web 依赖，缺失则退回已知目录。 */
function loadEsbuild() {
  const candidates = [path.join(ROOT, "web/node_modules/esbuild")];
  if (process.env.ESBUILD_DIR) candidates.unshift(path.join(process.env.ESBUILD_DIR, "esbuild"));
  for (const c of candidates) {
    try { return require(c); } catch (_) { /* 换下一个 */ }
  }
  throw new Error("找不到 esbuild：在 web/ 里装依赖，或用 ESBUILD_DIR 指定");
}
const esbuild = loadEsbuild();

function env(k) {
  return (fs.readFileSync(path.join(ROOT, ".env"), "utf8").split(/\r?\n/)
    .find((l) => l.startsWith(k + "=")) || "").slice(k.length + 1).trim();
}
const PASS = [], FAIL = [];
function check(name, ok, detail = "") {
  (ok ? PASS : FAIL).push(name);
  console.log(`${ok ? "  OK " : "  !! "} ${name}${detail ? `  [${detail}]` : ""}`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function loadTs(rel) {
  const out = esbuild.buildSync({
    entryPoints: [path.join(ROOT, "web/src", rel)],
    bundle: true, format: "cjs", write: false,
  });
  const mod = { exports: {} };
  new Function("module", "exports", out.outputFiles[0].text)(mod, mod.exports);
  return mod.exports;
}

async function joinSession(note, nickname) {
  const login = await fetch(BASE + "/api/admin/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: env("ADMIN_PASSWORD") }),
  });
  const adminCookie = (login.headers.get("set-cookie") || "").split(";")[0];
  for (let attempt = 1; attempt <= 4; attempt++) {
    const inv = await fetch(BASE + "/api/admin/invites", {
      method: "POST", headers: { "Content-Type": "application/json", Cookie: adminCookie },
      body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 30, note }),
    }).then((r) => r.json());
    const ctx = await fetch(BASE + "/api/join", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: inv.codes[0].toLowerCase(), nickname }),
    });
    const cookie = (ctx.headers.get("set-cookie") || "").split(";")[0];
    if (cookie) return cookie;
    console.log(`  .. join 被限流（第 ${attempt} 次），退避 75s`);
    await sleep(75000);
  }
  throw new Error("连续四次 join 都被限流");
}

/** 直接查库，确认工具结果确实以 role='tool' 落库 —— 不只看前端渲染。
 *  写临时脚本再执行：SQL 里有引号，塞进 -c "..." 会被 shell 吞掉。 */
function dbRoleOf(taskId) {
  const dbPath = path.join(ROOT, "data", "app.db");
  const c = require("child_process");
  const script = [
    "import sqlite3",
    `conn = sqlite3.connect(r'${dbPath}')`,
    `rows = conn.execute("SELECT role, substr(content,1,40) FROM messages WHERE task_id=? ORDER BY id", ('${taskId}',)).fetchall()`,
    "for role, content in rows:",
    "    print(role, '|', (content or '').replace(chr(10), ' '))",
    "print('--total--', len(rows))",
  ].join("\n");
  const tmp = path.join(require("os").tmpdir(), `p18_dbrole_${Date.now()}.py`);
  fs.writeFileSync(tmp, script, "utf8");
  try {
    return c.execSync(`"${PY}" "${tmp}"`, { encoding: "utf8" });
  } finally {
    fs.unlinkSync(tmp);
  }
}

(async () => {
  fs.mkdirSync(path.join(ROOT, "tests/artifacts"), { recursive: true });

  // Part A：静态确认前端确实有 role='tool' 渲染分支
  console.log("\n-- A：前端渲染分支存在性（静态） --");
  const msgSrc = fs.readFileSync(path.join(ROOT, "web/src/components/Message.tsx"), "utf8");
  check("Message.tsx 含 role==='tool' 分支", msgSrc.includes("if (m.role === \"tool\")"));
  check("role='tool' 渲染为 msg-tool 中性卡", msgSrc.includes("msg msg-tool") && msgSrc.includes('data-testid="msg-tool"'));
  check("旧 user 气泡分支仍保留（兜底老数据）", msgSrc.includes('data-testid="msg-user"'));

  // Part B：浏览器端到端
  console.log(`\n-- B：浏览器端到端（${BASE}） --`);
  const cookie = await joinSession("p18", "p18工具卡");
  const H = { "Content-Type": "application/json", Cookie: cookie };
  const task = await fetch(BASE + "/api/tasks", {
    method: "POST", headers: H, body: JSON.stringify({ mode: "writing", goal: "p18 工具角色卡" }),
  }).then((r) => r.json());
  const taskId = (task.task || {}).id || "";
  check("建任务", Boolean(taskId), taskId);

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const kv = cookie.split("=");
  const u = new URL(BASE);
  await context.addCookies([{ name: kv[0], value: kv[1], domain: u.hostname, path: "/" }]);
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });

  await page.goto(BASE + "/t/" + taskId, { waitUntil: "networkidle" });
  await page.waitForSelector("[data-testid=composer-input]");
  // rag_query 在未绑知识库的任务里必然报错 → 回注一条 error 结果，确定性高、不需联网
  await page.fill("[data-testid=composer-input]",
    '#fake {"tool_on": true, "tool_name": "rag_query", "tool_input": {"query": "测试回注"}}');
  await page.click("[data-testid=send-btn]");
  await page.waitForSelector('[data-testid=tool-card][data-status="done"]', { timeout: 30000 }).catch(() => {});
  await sleep(1200);

  // 刷新走「读库渲染」路径：回注消息由消息列表渲染，此时应走 role='tool' 分支
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForSelector("[data-testid=msg-ai]", { timeout: 15000 }).catch(() => {});
  await sleep(600);

  const toolCards = await page.locator("[data-testid=msg-tool]").count();
  check("工具结果渲染为 msg-tool 中性卡（role='tool' 分支生效）", toolCards >= 1, `${toolCards} 张`);

  const pureToolBubbles = await page.locator("[data-testid=msg-user][data-pure-tool]").count();
  check("不再冒充用户气泡（msg-user[data-pure-tool] = 0）", pureToolBubbles === 0, `${pureToolBubbles} 条`);

  const label = await page.locator("[data-testid=msg-tool] .msg-label").first().textContent().catch(() => "");
  check("中性卡标签含「工具结果」", (label || "").includes("工具结果"), (label || "").trim());

  const toolResultCards = await page.locator("[data-testid=msg-tool] [data-testid=tool-result-card]").count();
  check("卡内含 tool-result-card 子卡", toolResultCards >= 1, `${toolResultCards} 张`);

  const name = await page.locator("[data-testid=msg-tool] [data-testid=tool-result-card] .tool-result-name").first().textContent().catch(() => "");
  check("tool-result-card 显示具名工具（非空）", Boolean((name || "").trim()), (name || "").trim());

  const leak = await page.evaluate(() =>
    document.body.innerText.includes("<<<TOOL_RESULT")
      || document.body.innerText.includes("<<<END_TOOL_RESULT"));
  check("页面不出现内部标记（<<<TOOL_RESULT）", !leak);

  // 默认折叠态先截图（这才是用户实际看到的效果）
  const summaryClosed = await page.locator("[data-testid=msg-tool] [data-testid=tool-result-card] summary").first().isVisible().catch(() => false);
  check("tool-result-card 默认折叠可点", summaryClosed);
  const bodyHiddenByDefault = await page.locator("[data-testid=msg-tool] [data-testid=tool-result-card] .tool-result-body").first().isVisible().catch(() => true);
  check("默认折叠：原始 JSON 体不可见", bodyHiddenByDefault === false);
  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p18_tool_role_card_collapsed.png"), fullPage: false });

  // 展开看原始 JSON 仍在（排查用）
  await page.locator("[data-testid=msg-tool] [data-testid=tool-result-card] summary").first().click();
  await sleep(200);
  const bodyVisible = await page.locator("[data-testid=msg-tool] [data-testid=tool-result-card] .tool-result-body").first().isVisible().catch(() => false);
  check("展开可见原始 JSON 体", bodyVisible);

  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p18_tool_role_card.png"), fullPage: false });

  // Part C：直查库坐实落库角色
  console.log("\n-- C：落库角色核验（直查 data/app.db） --");
  try {
    const rows = dbRoleOf(taskId);
    console.log(rows);
    const hasToolRole = rows.split(/\r?\n/).some((l) => l.trim().startsWith("tool"));
    check("数据库工具结果行 role='tool'", hasToolRole, rows.split(/\r?\n/).find((l) => l.startsWith("tool")) || "");
  } catch (e) {
    check("数据库工具结果行 role='tool'", false, String(e).slice(0, 120));
  }

  check("浏览器无 console error", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));
  await browser.close();

  console.log(`\n== p18: ${PASS.length} 过 / ${FAIL.length} 败 ==`);
  if (FAIL.length) { console.log("失败项：", FAIL); process.exit(1); }
})().catch((e) => { console.error("P18 FAIL", e); process.exit(1); });
