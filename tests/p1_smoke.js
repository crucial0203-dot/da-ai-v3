/** p1 冒烟：邀请码进站 → 首页 composer → 三主题切换 → 四个页面渲染。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p1_smoke.js
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

async function apiAdmin(path_, body) {
  // 管理员 API 走独立 cookie（浏览器之外的 fetch 带 cookie jar）
  const res = await fetch(BASE + path_, {
    method: body ? "POST" : "GET",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  return res;
}

async function main() {
  // 准备一个新邀请码
  const login = await apiAdmin("/api/admin/login", { password: env("ADMIN_PASSWORD") });
  if (!login.ok) throw new Error("管理员登录失败");
  const setCookie = login.headers.get("set-cookie") || "";
  const adminCookie = setCookie.split(";")[0];
  const inv = await fetch(BASE + "/api/admin/invites", {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: adminCookie },
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 20, note: "p1" }),
  }).then((r) => r.json());
  const code = inv.codes[0];

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(15000);

  // [1] 邀请码进站
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  check("出认证页", await page.locator("[data-testid=auth-form]").isVisible());
  await page.fill("#code", code.toLowerCase());
  await page.fill("#nickname", "p1冒烟");
  await page.click("[data-testid=auth-submit]");
  await page.waitForSelector("[data-testid=composer-input]");
  check("进站后见首页 composer", true);
  check("侧栏渲染", await page.locator("[data-testid=sidebar]").isVisible());
  check("模式卡渲染", (await page.locator("[data-testid=mode-chips] .mode-chip").count()) >= 4);

  // [2] 三主题（侧栏底部的主题 chips）
  const setTheme = async (label, want) => {
    await page.locator(`button[title="${label}主题"]`).first().click();
    await page.waitForTimeout(300);
    const theme = await page.evaluate(() => document.documentElement.dataset.theme);
    check(`主题切到 ${want}`, theme === want, `got ${theme}`);
  };
  await setTheme("深色", "dark");
  await setTheme("纯黑", "oled");
  await setTheme("浅色", "light");

  // [3] 页面渲染
  await page.click('a:has-text("技能")');
  await page.waitForSelector("[data-testid=skill-search]");
  check("技能中心渲染", true);
  await page.click('a:text-is("库")');
  await page.waitForSelector("h1:text-is('库')");
  check("库页渲染", true);
  await page.click('a:has-text("设置")');
  await page.waitForSelector("h1:has-text('设置')");
  check("设置页渲染", true);
  await page.click('a:has-text("对话")');
  await page.waitForSelector("[data-testid=composer-input]");
  check("回对话首页", true);

  // [4] 侧栏折叠
  await page.click('button[title="收起侧栏"]');
  await page.waitForTimeout(200);
  check("侧栏可折叠", await page.locator('button[title="展开侧栏"]').isVisible());

  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p1_smoke.png"), fullPage: true });
  await browser.close();

  console.log(`\n== p1: ${PASS.length} 过 / ${FAIL.length} 败 ==`);
  if (FAIL.length) { FAIL.forEach((f) => console.log("  - " + f)); process.exit(1); }
}

main().catch((err) => { console.error(err); process.exit(2); });
