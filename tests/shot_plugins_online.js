// 线上插件页截图验收（一次性脚本）：sharecode 门禁 → 邀请码登录 → 侧栏进 /plugins
const { chromium } = require("playwright-core");
const fs = require("fs"), path = require("path");
const ROOT = "D:/workbuddyGZ/da-ai-v3";

function env(k) {
  return (fs.readFileSync(path.join(ROOT, ".env"), "utf8").split(/\r?\n/)
    .find((l) => l.startsWith(k + "=")) || "").slice(k.length + 1).trim();
}

(async () => {
  const base = "https://da-ai.app.workbuddy.host/?sharecode=2GAE2PHAAJ";
  const code = env("SMOKE_INVITE") || process.argv[2] || "7CAM8SNE";

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(20000);

  // [1] 邀请码进站
  await page.goto(base, { waitUntil: "networkidle", timeout: 60000 });
  if (await page.locator("[data-testid=auth-form]").isVisible().catch(() => false)) {
    await page.fill("#code", code);
    await page.fill("#nickname", "线上验收");
    await page.click("[data-testid=auth-submit]");
    await page.waitForSelector("[data-testid=composer-input]");
    console.log("login-ok");
  } else {
    console.log("already-logged-in");
  }

  // [2] 侧栏进插件页（客户端导航，保证 SPA 路由生效）
  const pluginsResp = page.waitForResponse(
    (r) => r.url().includes("/api/plugins"),
    { timeout: 30000 }
  ).catch(() => null);
  await page.click('a[href="/plugins"]');
  await page.waitForSelector('[data-testid="plugins-page"]');
  const resp = await pluginsResp;
  console.log(`api-plugins-status=${resp ? resp.status() : "no-response"}`);
  if (resp) console.log(`api-plugins-body=${(await resp.text()).slice(0, 300)}`);
  // 等后端插件区从「加载中…」落定
  await page.waitForSelector('[data-testid="backend-plugin"], .muted:has-text("没有已加载")', { timeout: 15000 }).catch(() => {});

  const cards = await page.locator('[data-testid="plugin-card"]').count();
  const slots = await page.locator('[data-testid="slot-row"]').count();
  const backend = await page.locator('[data-testid="backend-plugin"]').count();
  const nav = await page.locator('a[href="/plugins"]').count();
  console.log(`plugin-cards=${cards} slot-rows=${slots} backend-plugins=${backend} sidebar-entry=${nav}`);
  if (cards < 3 || slots < 5 || backend < 1 || nav < 1) {
    console.error("FAIL: 插件页元素数量不符");
    await page.screenshot({ path: path.join(ROOT, "tests/online_plugins_fail.png"), fullPage: true });
    process.exit(1);
  }

  await page.screenshot({ path: path.join(ROOT, "tests/online_plugins.png"), fullPage: true });
  console.log("ONLINE PLUGINS OK");
  await browser.close();
})();
