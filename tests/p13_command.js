/** p13 命令面板（P2-7）：mod+k 呼出、三类 section、稳定 value、过滤、Enter 跳转、Esc 关闭。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p13_command.js
 */
const { chromium } = require("playwright-core");
const fs = require("fs"), path = require("path");
const ROOT = "D:/workbuddyGZ/da-ai-v3";
const BASE = "http://127.0.0.1:8020";

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

async function main() {
  const login = await fetch(BASE + "/api/admin/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: env("ADMIN_PASSWORD") }),
  });
  const adminCookie = (login.headers.get("set-cookie") || "").split(";")[0];
  const inv = await fetch(BASE + "/api/admin/invites", {
    method: "POST", headers: { "Content-Type": "application/json", Cookie: adminCookie },
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 30, note: "p13cmd" }),
  }).then((r) => r.json());
  const code = inv.codes[0];

  // 先用 API 建一个任务（同会话），供「最近任务」section 有内容
  const join = await fetch(BASE + "/api/join", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: code.toLowerCase(), nickname: "p13cmd" }),
  });
  const cookie = (join.headers.get("set-cookie") || "").split(";")[0];
  const created = await fetch(BASE + "/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: cookie },
    body: JSON.stringify({ mode: "chat", goal: "p13 命令面板测试任务" }),
  }).then((r) => r.json());
  const taskId = created.task?.id || "";

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const sid = cookie.split("=")[1];
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.addCookies([{ name: cookie.split("=")[0], value: sid, domain: "127.0.0.1", path: "/" }]);
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.waitForSelector("[data-testid=composer-input]");

  // [1] mod+k 呼出
  await page.keyboard.press("Control+k");
  await page.waitForSelector("[data-testid=command-palette]");
  check("[1] mod+k 呼出面板", true);

  // [2] 三类 section + 稳定 value
  const headings = await page.locator("[data-testid=cmdk-list] .cmdk-heading").allInnerTexts();
  check("[2] 有「操作」与「导航」section", headings.includes("操作") && headings.includes("导航"), headings.join("/"));
  const navSkills = await page.locator('[data-testid=cmdk-item][data-value="nav:/skills"]').count();
  check("[2] item value 稳定唯一（nav:/skills）", navSkills === 1);
  const taskItem = page.locator(`[data-testid=cmdk-item][data-value="task:${taskId}"]`);
  check("[2] 最近任务 section 带刚建的任务", (await taskItem.count()) === 1, taskId);

  // [3] 过滤：输入「画布」只剩导航项
  await page.fill("[data-testid=cmdk-input]", "画布");
  await sleep(200);
  const items = await page.locator("[data-testid=cmdk-item]").allInnerTexts();
  check("[3] 过滤后只剩画布相关项", items.length >= 1 && items.every((t) => t.includes("画布")), items.join("|"));

  // [4] Enter 跳转 /canvas
  await page.keyboard.press("Enter");
  await sleep(600);
  const onCanvas = await page.locator("[data-testid=canvas-flow]").count();
  check("[4] Enter 跳到画布页", onCanvas === 1);
  check("[4] 跳转后面板关闭", (await page.locator("[data-testid=command-palette]").count()) === 0);

  // [5] Esc 关闭
  await page.keyboard.press("Control+k");
  await page.waitForSelector("[data-testid=command-palette]");
  await page.keyboard.press("Escape");
  await sleep(200);
  check("[5] Esc 关闭面板", (await page.locator("[data-testid=command-palette]").count()) === 0);

  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p13_command.png"), fullPage: true });
  await browser.close();
  console.log(`\n== p13cmd: ${PASS.length} 过 / ${FAIL.length} 败 ==`);
  if (FAIL.length) { FAIL.forEach((f) => console.log("  - " + f)); process.exit(1); }
}

main().catch((err) => { console.error(err); process.exit(2); });
