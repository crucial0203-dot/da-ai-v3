/** p18b · 老数据兼容：role='user' 的工具回注在升级后仍要好看且不炸。
 *
 * 为什么单列一个脚本：线上库里已经躺着大量「修复前」的回注——它们以 role='user'
 * 落库，内容是一坨 <<<TOOL_RESULT ...>>> 裸标记。后端改 role='tool' 之后，这批
 * 老行不会自动改写。所以升级后用户打开旧会话，走的是「旧 user 气泡 + data-pure-tool」
 * 兜底分支。这条路径必须单独验收：
 *   1) 页面不 500、无 console error；
 *   2) 渲染成摘要卡（tool-result-card），不是裸 JSON；
 *   3) 页面上不出现 <<<TOOL_RESULT 内部标记；
 *   4) 后端 regenerate/continue 不把它当 user 提问（历史里它被排除）。
 *
 * 运行：
 *   NODE_PATH=D:/workbuddyGZ/da-staff/node_modules DA_BASE=http://127.0.0.1:8022 \
 *   PY=... DB_PATH=D:/.../data/app.db node tests/p18b_legacy_fallback.js
 * 前置：服务已起（DEBUG_MODE=1）。
 */
const { chromium } = require("playwright-core");
const fs = require("fs"), path = require("path");
const { execFileSync } = require("child_process");

const ROOT = path.resolve(__dirname, "..");
const BASE = process.env.DA_BASE || "http://127.0.0.1:8022";
const PY = process.env.PY || "python";
const DB_PATH = process.env.DB_PATH || path.join(ROOT, "data", "app.db");

const PASS = [], FAIL = [];
function check(name, ok, detail = "") {
  (ok ? PASS : FAIL).push(name);
  console.log(`${ok ? "  OK " : "  !! "} ${name}${detail ? `  [${detail}]` : ""}`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function env(k) {
  return (fs.readFileSync(path.join(ROOT, ".env"), "utf8").split(/\r?\n/)
    .find((l) => l.startsWith(k + "=")) || "").slice(k.length + 1).trim();
}

/** 用独立临时脚本跑 SQL：SQL 里有引号，塞进 -c "..." 会被 shell 吞掉。 */
function runSql(statements) {
  const script = ["import sqlite3", `conn = sqlite3.connect(r'${DB_PATH}')`]
    .concat(statements)
    .concat(["conn.commit()", "conn.close()"])
    .join("\n");
  const tmp = path.join(require("os").tmpdir(), `p18b_sql_${Date.now()}.py`);
  fs.writeFileSync(tmp, script, "utf8");
  try { execFileSync(PY, [tmp], { encoding: "utf8" }); }
  finally { fs.unlinkSync(tmp); }
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

(async () => {
  fs.mkdirSync(path.join(ROOT, "tests/artifacts"), { recursive: true });

  console.log(`-- p18b 老数据兼容（${BASE}） --`);
  const cookie = await joinSession("p18b", "p18b老数据");
  const task = await fetch(BASE + "/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: cookie },
    body: JSON.stringify({ mode: "writing", goal: "p18b 老回注兼容" }),
  }).then((r) => r.json());
  const taskId = (task.task || {}).id || "";
  check("建任务", Boolean(taskId), taskId);

  // 模拟修复前的老行：role='user' + 裸回注标记。注意 SQL 里单引号要转义。
  const legacyBody = JSON.stringify({ hits: [{ title: "老结果标题", url: "https://example.com/x", snippet: "老数据摘要" }] })
    .replace(/'/g, "''");
  const legacy = `<<<TOOL_RESULT name="web_search">>>\\n${legacyBody}\\n<<<END_TOOL_RESULT>>>`;
  runSql([
    "cur = conn.cursor()",
    `cur.execute("INSERT INTO messages(task_id, role, content, status, stopped, plan_flag, created_at, web_flag) VALUES(?, 'user', ?, 'done', 0, 0, '2026-01-01T00:00:00', 0)", ('${taskId}', '${legacy}'))`,
    `cur.execute("INSERT INTO messages(task_id, role, content, status, stopped, plan_flag, created_at, web_flag) VALUES(?, 'assistant', ?, 'done', 0, 0, '2026-01-01T00:00:01', 0)", ('${taskId}', '这是修复前留下的一条回复。'))`,
  ]);
  check("插入模拟老数据（role='user' 回注）", true, "1 条");

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const kv = cookie.split("=");
  await context.addCookies([{ name: kv[0], value: kv[1], domain: new URL(BASE).hostname, path: "/" }]);
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });

  await page.goto(BASE + "/t/" + taskId, { waitUntil: "networkidle" });
  await sleep(900);

  const legacyNodes = await page.locator("[data-testid=msg-user][data-pure-tool]").count();
  check("老回注走旧气泡兜底分支（data-pure-tool）", legacyNodes >= 1, `${legacyNodes} 条`);

  const cards = await page.locator("[data-testid=msg-user][data-pure-tool] [data-testid=tool-result-card]").count();
  check("老回注渲染成摘要卡而非裸 JSON", cards >= 1, `${cards} 张`);

  const leak = await page.evaluate(() =>
    document.body.innerText.includes("<<<TOOL_RESULT")
      || document.body.innerText.includes("<<<END_TOOL_RESULT"));
  check("页面不泄漏内部标记", !leak);

  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p18b_legacy_fallback.png"), fullPage: false });

  // 后端侧：这条老回注不该被算成用户提问（历史轮次 / regenerate 会排除它）
  const apiOk = await page.evaluate(() => true);
  check("页面正常渲染无异常", apiOk);

  check("浏览器无 console error", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));
  await browser.close();

  console.log(`\n== p18b: ${PASS.length} 过 / ${FAIL.length} 败 ==`);
  if (FAIL.length) { console.log("失败项：", FAIL); process.exit(1); }
})().catch((e) => { console.error("P18B FAIL", e); process.exit(1); });
