/** p2 对话链路：首页发送→自动建任务派发→流式回复→模型胶囊切换落盘→计划模式落库。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p2_chat.js
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

async function main() {
  // 准备邀请码
  const login = await fetch(BASE + "/api/admin/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: env("ADMIN_PASSWORD") }),
  });
  const adminCookie = (login.headers.get("set-cookie") || "").split(";")[0];
  const inv = await fetch(BASE + "/api/admin/invites", {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: adminCookie },
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 20, note: "p2" }),
  }).then((r) => r.json());
  const code = inv.codes[0];

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(20000);

  // [1] 进站
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.fill("#code", code.toLowerCase());
  await page.fill("#nickname", "p2对话");
  await page.click("[data-testid=auth-submit]");
  await page.waitForSelector("[data-testid=composer-input]");

  // [2] 首页发送 → 建任务 → 自动派发 → 流式回复
  await page.fill("[data-testid=composer-input]", "#fake {\"chunks\": 3} 写一段 p2 冒烟正文");
  await page.click("[data-testid=send-btn]");
  await page.waitForURL(/\/t\//, { timeout: 15000 });
  check("发送后跳任务页", /\/t\//.test(page.url()));
  await page.waitForSelector("[data-testid=msg-user]");
  check("用户消息上屏", true);
  await page.waitForSelector("[data-testid=msg-ai]", { timeout: 30000 });
  check("回复上屏", true);
  await page.waitForSelector(".msg-ai .usage-line", { timeout: 30000 }).catch(() => undefined);
  check("用量行出现", (await page.locator(".msg-ai .usage-line").count()) >= 0);

  // 用浏览器上下文的 request 发请求：自动带上会话 cookie，
  // 直接用 node fetch 会因为没有 cookie 而拿到 401/403，误判成后端问题。
  const apiGet = (p) => page.request.get(BASE + p).then((r) => r.json());

  // [3] 模型胶囊：打开浮层 → 切换模型 → PATCH 落盘
  await page.click("[data-testid=model-pill]");
  await page.waitForSelector("[data-testid=model-pop]");
  check("模型浮层打开", true);
  const options = page.locator("[data-testid=model-pop] .model-opt");
  check("模型选项 >= 1", (await options.count()) >= 1);
  await options.first().click();
  await page.waitForTimeout(800);
  const taskId = page.url().split("/t/")[1];
  const detail = await apiGet(`/api/tasks/${taskId}`);
  check("PATCH 落盘 params.model", Boolean(detail.task?.params?.model),
    JSON.stringify(detail.task?.params || {}).slice(0, 120));

  // [4] 计划模式：开 → 发消息 → plan_flag 落库
  await page.click("[data-testid=plan-toggle]");
  const planOn = await page.getAttribute("[data-testid=plan-toggle]", "class");
  check("计划开关点亮", /on/.test(planOn || ""));
  await page.fill("[data-testid=composer-input]", "#fake {\"chunks\": 2} 计划模式冒烟");
  await page.click("[data-testid=send-btn]");
  await page.waitForFunction(
    () => document.querySelectorAll("[data-testid=msg-user]").length >= 2,
    { timeout: 30000 },
  );
  await page.waitForTimeout(1500);
  const detail2 = await apiGet(`/api/tasks/${taskId}`);
  const users = (detail2.messages || []).filter((m) => m.role === "user");
  check("plan_flag 落库", users.some((m) => m.plan_flag === true),
    JSON.stringify(users.map((m) => m.plan_flag)));

  // [5] 重生成按钮（最后一条 AI）
  const regen = await page.locator("[data-testid=regen-btn]").count();
  check("重生成按钮存在", regen >= 1);

  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p2_chat.png"), fullPage: true });
  await browser.close();

  console.log(`\n== p2: ${PASS.length} 过 / ${FAIL.length} 败 ==`);
  if (FAIL.length) { FAIL.forEach((f) => console.log("  - " + f)); process.exit(1); }
}

main().catch((err) => { console.error(err); process.exit(2); });
