/** p19 · 线上专项：工具结果卡在公网环境是否也是「中性卡」。
 *
 * 本地 p18 已证代码路径；这里证**线上跑的确实是这份代码**：真实模型调用工具后
 * 落库角色正确、刷新后渲染成 msg-tool 而不是用户气泡。
 *
 * 与 p18 的差别：线上没有 FAKE_UPSTREAM，不能用 #fake 造工具调用，只能让真实模型
 * 去调一个确定性工具（current_time：低危免审批、不联网、不耗外部资源）。
 *
 * 实现要点：用 Playwright 的 APIRequestContext 而不是 node fetch —— 线上平台会
 * 下发多个 Set-Cookie，node fetch 会把它们合并成一条，手工切分取到错误的 cookie，
 * 后续管理员接口就 403。APIRequestContext 与页面共享 cookie，行为跟真实浏览器一致。
 *
 * 跑法：
 *   NODE_PATH=D:/workbuddyGZ/da-staff/node_modules \
 *   node tests/p19_online_tool_card.js [baseUrl] [sharecode]
 */
const { chromium } = require("playwright-core");
const fs = require("fs"), path = require("path");

const ROOT = path.resolve(__dirname, "..");
const BASE = (process.argv[2] || "https://da-ai.app.workbuddy.host").replace(/\/$/, "");
const SHARE = process.argv[3] || "2GAE2PHAAJ";

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
/** 线上每个请求都要带 sharecode，否则 403 */
function u(p) {
  return `${BASE}${p}${p.includes("?") ? "&" : "?"}sharecode=${SHARE}`;
}

(async () => {
  fs.mkdirSync(path.join(ROOT, "tests/artifacts"), { recursive: true });
  console.log(`-- p19 线上工具卡（${BASE}） --`);

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const api = context.request;

  // 分享门禁对照：无参必须被拒（否则后面测的可能是「门禁失效」）
  const noShare = await api.get(BASE + "/api/meta").catch(() => null);
  check("无 sharecode 被门禁拦（对照）", !noShare || noShare.status() === 403,
    `HTTP ${noShare ? noShare.status() : "ERR"}`);

  const login = await api.post(u("/api/admin/login"), { data: { password: env("ADMIN_PASSWORD") } });
  check("管理员登录", login.status() === 200, `HTTP ${login.status()}`);

  const invRes = await api.post(u("/api/admin/invites"),
    { data: { count: 1, max_uses: 3, days: 3, daily_quota: 20, note: "p19 线上工具卡" } });
  const inv = await invRes.json().catch(() => ({}));
  const code = (inv.codes && inv.codes[0] || "").toLowerCase();
  check("建邀请码", Boolean(code), code || `HTTP ${invRes.status()} ${JSON.stringify(inv).slice(0, 80)}`);

  // 换一个干净上下文建访客，免得管理员 cookie 混进访客判定
  const guestCtx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const gapi = guestCtx.request;
  const join = await gapi.post(u("/api/join"), { data: { code, nickname: "p19工具卡" } });
  check("凭码进入", join.status() === 200, `HTTP ${join.status()}`);

  const taskRes = await gapi.post(u("/api/tasks"), { data: { mode: "writing", goal: "p19 线上工具卡" } });
  const taskBody = await taskRes.json().catch(() => ({}));
  const taskId = (taskBody.task || {}).id || "";
  check("建任务", Boolean(taskId), taskId || `HTTP ${taskRes.status()}`);

  const page = await guestCtx.newPage();
  page.setDefaultTimeout(60000);
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });

  await page.goto(u("/t/" + taskId), { waitUntil: "networkidle", timeout: 60000 });
  await page.waitForSelector("[data-testid=composer-input]", { timeout: 30000 });

  // 让真实模型调一个确定性工具：current_time（低危免审批、不联网）
  await page.fill("[data-testid=composer-input]",
    "请调用 current_time 工具告诉我现在几点，然后用一句话回答。");
  await page.click("[data-testid=send-btn]");

  const gotTool = await page.waitForSelector('[data-testid=tool-card][data-status="done"]', { timeout: 90000 })
    .then(() => true).catch(() => false);
  check("真实模型触发了工具调用（tool-card done）", gotTool);
  await sleep(1500);

  await page.reload({ waitUntil: "networkidle", timeout: 60000 });
  await page.waitForSelector("[data-testid=msg-ai]", { timeout: 30000 }).catch(() => {});
  await sleep(800);

  if (gotTool) {
    const toolCards = await page.locator("[data-testid=msg-tool]").count();
    check("刷新后渲染为 msg-tool 中性卡", toolCards >= 1, `${toolCards} 张`);

    const pureBubbles = await page.locator("[data-testid=msg-user][data-pure-tool]").count();
    check("不冒充用户气泡（data-pure-tool = 0）", pureBubbles === 0, `${pureBubbles} 条`);

    const label = await page.locator("[data-testid=msg-tool] .msg-label").first().textContent().catch(() => "");
    check("标签含「工具结果」", (label || "").includes("工具结果"), (label || "").trim());

    const name = await page.locator("[data-testid=msg-tool] .tool-result-name").first().textContent().catch(() => "");
    check("显示具名工具", Boolean((name || "").trim()), (name || "").trim());

    const leak = await page.evaluate(() =>
      document.body.innerText.includes("<<<TOOL_RESULT") || document.body.innerText.includes("<<<END_TOOL_RESULT"));
    check("页面不泄漏内部标记", !leak);

    await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p19_online_tool_card.png"), fullPage: false });
  } else {
    console.log("  .. 模型未触发工具调用，跳过渲染断言（该路径本地 p18 已覆盖）");
  }

  check("浏览器无 console error", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));
  await browser.close();

  console.log(`\n== p19: ${PASS.length} 过 / ${FAIL.length} 败 ==`);
  if (FAIL.length) { console.log("失败项：", FAIL); process.exit(1); }
})().catch((e) => { console.error("P19 FAIL", e); process.exit(1); });
