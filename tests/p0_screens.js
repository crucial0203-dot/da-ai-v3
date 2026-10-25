/** p0 巡检：各主要页面截图，供人工目视核对视觉。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p0_screens.js
 */
const { chromium } = require("playwright-core");
const fs = require("fs"), path = require("path");
const ROOT = "D:/workbuddyGZ/da-ai-v3";
const BASE = "http://127.0.0.1:8020";
const OUT = path.join(ROOT, "tests/artifacts");

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
    method: "POST", headers: { "Content-Type": "application/json", Cookie: adminCookie },
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 30, note: "p0" }),
  }).then((r) => r.json());
  const code = inv.codes[0];

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(20000);

  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.fill("#code", code.toLowerCase());
  await page.fill("#nickname", "p0巡检");
  await page.click("[data-testid=auth-submit]");
  await page.waitForSelector("[data-testid=composer-input]");

  const shot = async (name) => {
    await page.waitForTimeout(700);
    await page.screenshot({ path: path.join(OUT, `p0_${name}.png`), fullPage: true });
    console.log("  shot:", name);
  };

  await shot("01_home");

  // 技能中心
  await page.click('a:has-text("技能")');
  await page.waitForSelector("[data-testid=skill-search]");
  await shot("02_skills");

  // 库
  await page.click('a:text-is("库")');
  await page.waitForTimeout(900);
  await shot("03_library");

  // 设置
  await page.click('a:has-text("设置")');
  await page.waitForTimeout(900);
  await shot("04_settings");

  // 管理台（访客无权，看是否优雅降级）
  await page.goto(BASE + "/admin", { waitUntil: "networkidle" });
  await page.waitForTimeout(700);
  await shot("05_admin");

  // 深色主题下的首页
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.waitForSelector("[data-testid=composer-input]");
  await page.locator('button[title="深色主题"]').first().click();
  await shot("06_home_dark");

  // 任务页：发一条消息看消息流
  await page.fill("[data-testid=composer-input]", "#fake {\"chunks\": 4} 巡检用正文");
  await page.click("[data-testid=send-btn]");
  await page.waitForURL(/\/t\//, { timeout: 15000 });
  await page.waitForSelector("[data-testid=msg-ai]", { timeout: 30000 });
  await shot("07_task_dark");

  await browser.close();
  console.log("\n== p0 巡检截图完成 ==");
}

main().catch((err) => { console.error(err); process.exit(2); });
