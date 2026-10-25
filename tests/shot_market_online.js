// 线上插件市场验收：sharecode 门禁 → 邀请码登录 → 市场页 → 装主题即时换肤 → 截图
const { chromium } = require("playwright-core");
const path = require("path");
const ROOT = "D:/workbuddyGZ/da-ai-v3";
const BASE = "https://da-ai.app.workbuddy.host/?sharecode=2GAE2PHAAJ";
const CODE = process.argv[2] || "7CAM8SNE";

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(25000);

  await page.goto(BASE, { waitUntil: "networkidle", timeout: 60000 });
  if (await page.locator("[data-testid=auth-form]").isVisible().catch(() => false)) {
    await page.fill("#code", CODE);
    await page.fill("#nickname", "线上验收");
    await page.click("[data-testid=auth-submit]");
  }
  await page.waitForSelector("[data-testid=composer-input]");
  console.log("login-ok");

  // 清理安装态保证可重复（浏览器会话内直接调 API）
  await page.evaluate(async () => {
    for (const id of ["ui.daily-quote", "theme.deep-space"]) {
      await fetch("/api/market/uninstall", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      }).catch(() => undefined);
    }
  });

  // 市场目录接口
  const cat = await page.evaluate(async () => {
    const r = await fetch("/api/market/catalog");
    return { status: r.status, count: (await r.json()).count };
  });
  console.log(`catalog=${JSON.stringify(cat)}`);
  if (cat.status !== 200 || cat.count < 9) throw new Error("市场目录异常");

  // 侧栏进市场页（等待接口返回后再断言，避免竞态误判）
  const respP = page.waitForResponse((r) => r.url().includes("/api/market/catalog"), { timeout: 30000 }).catch(() => null);
  await page.click('a[href="/plugins"]');
  await page.waitForSelector('[data-testid="plugins-page"]');
  await page.waitForSelector('[data-testid="plugin-card"]');
  await respP;
  const cards = await page.locator('[data-testid="plugin-card"]').count();
  console.log(`discover-cards=${cards}`);
  if (cards < 9) throw new Error("发现页卡片不足 9");
  await page.screenshot({ path: path.join(ROOT, "tests/online_market_discover.png"), fullPage: true });

  // 安装主题「深空蓝」→ 全站即时换肤
  await page.click('[data-testid="market-tab-themes"]');
  await page.waitForTimeout(300);
  const deepSpace = page.locator('[data-testid="plugin-card"]', { hasText: "深空蓝" });
  await deepSpace.locator('[data-testid="market-install-btn"]').click();
  await page.waitForSelector('[data-testid="market-confirm-ok"]');
  await page.click('[data-testid="market-confirm-ok"]');
  await page.waitForFunction(() => document.documentElement.getAttribute("data-market-theme") === "theme.deep-space", null, { timeout: 20000 });
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  console.log(`theme-applied bg=${bg}`);
  await page.screenshot({ path: path.join(ROOT, "tests/online_market_theme.png"), fullPage: true });

  // UI 模块「每日一言」→ 侧栏卡片
  await page.click('[data-testid="market-tab-discover"]');
  const quote = page.locator('[data-testid="plugin-card"]', { hasText: "每日一言" });
  await quote.locator('[data-testid="market-install-btn"]').click();
  await page.click('[data-testid="market-confirm-ok"]');
  await page.waitForSelector('[data-testid="plugin-daily-quote"]', { timeout: 20000 }).catch(() => {});
  const uiOk = await page.locator('[data-testid="plugin-daily-quote"]').count();
  console.log(`ui-module-in-sidebar=${uiOk}`);
  if (!uiOk) throw new Error("UI 模块未在侧栏渲染");
  await page.screenshot({ path: path.join(ROOT, "tests/online_market_ui_module.png"), fullPage: true });

  console.log("ONLINE MARKET OK");
  await browser.close();
})();
