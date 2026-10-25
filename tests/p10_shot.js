const { chromium } = require("playwright-core");
const fs = require("fs"), path = require("path");
const ROOT = "D:/workbuddyGZ/da-ai-v3";
const BASE = "http://127.0.0.1:8020";
function env(k) {
  return (fs.readFileSync(path.join(ROOT, ".env"), "utf8").split(/\r?\n/)
    .find((l) => l.startsWith(k + "=")) || "").slice(k.length + 1).trim();
}
async function main() {
  const login = await fetch(BASE + "/api/admin/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: env("ADMIN_PASSWORD") }),
  });
  const adminCookie = (login.headers.get("set-cookie") || "").split(";")[0];
  const inv = await fetch(BASE + "/api/admin/invites", {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: adminCookie },
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 50, note: "p10shot" }),
  }).then((r) => r.json());
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.fill("#code", inv.codes[0].toLowerCase());
  await page.fill("#nickname", "p10截图");
  await page.click("[data-testid=auth-submit]");
  await page.waitForSelector("[data-testid=history-list]");

  async function send(text) {
    const r = await page.request.post(BASE + "/api/tasks", {
      data: { goal: "p10 截图", mode: "research", title: "p10shot" },
    });
    const task = await r.json();
    const id = task.id || (task.task || {}).id;
    await page.goto(BASE + "/t/" + id, { waitUntil: "networkidle" });
    await page.waitForSelector("[data-testid=composer-input]");
    await page.fill("[data-testid=composer-input]", text);
    await page.click("[data-testid=send-btn]");
  }

  // 场景 1：单卡 running 态 + 参数展开 + 思考块展开
  await send('#fake #tool {"tool_name":"web_search","tool_input":{"query":"行政协议诉讼 2024 判例"},"delay_ms":4000}');
  await page.waitForSelector("[data-testid=tool-card][data-kind=search]", { timeout: 20000 });
  await page.waitForTimeout(600);
  await page.screenshot({ path: "tests/p10_shot_single.png", fullPage: false });

  // 场景 2：两组连续同类折叠 + 组内展开
  await send('#fake #tool {"tool_name":"web_search","tool_rounds":2,"delay_ms":4000}');
  await page.waitForSelector("[data-testid=tool-group][data-kind=search]", { timeout: 25000 });
  await page.waitForTimeout(800);
  await page.locator("[data-testid=tool-group] > summary").first().click();
  await page.waitForTimeout(400);
  await page.screenshot({ path: "tests/p10_shot_group.png", fullPage: false });

  await browser.close();
  console.log("screenshots done");
}
main().catch((e) => { console.error("shot err:", e); process.exit(1); });
