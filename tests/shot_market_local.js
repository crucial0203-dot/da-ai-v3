// 本地市场页视觉验收：登录 → 市场页 → 装主题即时换肤 → 装 UI 模块即上侧栏
const { chromium } = require("playwright-core");
const path = require("path");
const ROOT = "D:/workbuddyGZ/da-ai-v3";
const BASE = "http://127.0.0.1:8020";
const CODE = process.argv[2] || "P3HQ8WYK";

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(20000);

  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  if (await page.locator("[data-testid=auth-form]").isVisible().catch(() => false)) {
    await page.fill("#code", CODE);
    await page.fill("#nickname", "市场验收");
    await page.click("[data-testid=auth-submit]");
  }
  await page.waitForSelector("[data-testid=composer-input]");

  // 清理上次安装态，保证脚本可重复跑（浏览器带会话 cookie 直接调 API）
  await page.evaluate(async () => {
    for (const id of ["ui.daily-quote", "theme.deep-space"]) {
      await fetch("/api/market/uninstall", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      }).catch(() => undefined);
    }
  });

  await page.click('a[href="/plugins"]');
  await page.waitForSelector('[data-testid="plugins-page"]');
  await page.waitForSelector('[data-testid="plugin-card"]');
  const cards = await page.locator('[data-testid="plugin-card"]').count();
  console.log(`discover-cards=${cards}`);
  if (cards < 9) throw new Error("发现页卡片不足 9");
  await page.screenshot({ path: path.join(ROOT, "tests/local_market_discover.png"), fullPage: true });

  // 主题 tab + 安装「深空蓝」即时换肤
  await page.click('[data-testid="market-tab-themes"]');
  await page.waitForTimeout(300);
  await page.screenshot({ path: path.join(ROOT, "tests/local_market_themes.png"), fullPage: true });
  const deepSpace = page.locator('[data-testid="plugin-card"]', { hasText: "深空蓝" });
  await deepSpace.locator('[data-testid="market-install-btn"]').click();
  await page.waitForSelector('[data-testid="market-confirm-ok"]');
  await page.click('[data-testid="market-confirm-ok"]');
  await page.waitForFunction(() => document.documentElement.getAttribute("data-market-theme") === "theme.deep-space", null, { timeout: 15000 });
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  console.log(`theme-applied bg=${bg}`);
  await page.screenshot({ path: path.join(ROOT, "tests/local_market_theme_applied.png"), fullPage: true });

  // 停用主题恢复基础主题
  await deepSpace.locator('[data-testid="market-toggle-btn"]').click();
  await page.waitForFunction(() => !document.documentElement.getAttribute("data-market-theme"), null, { timeout: 15000 });
  console.log("theme-cleared");

  // UI 模块「每日一言」→ 侧栏出现卡片（用模块内测试锚点，不依赖轮换引言文本）
  await page.click('[data-testid="market-tab-discover"]');
  const quote = page.locator('[data-testid="plugin-card"]', { hasText: "每日一言" });
  await quote.locator('[data-testid="market-install-btn"]').click();
  await page.click('[data-testid="market-confirm-ok"]');
  await page.waitForSelector('[data-testid="plugin-daily-quote"]', { timeout: 15000 }).catch(() => {});
  const sidebarHas = await page.locator('[data-testid="plugin-daily-quote"]').count();
  console.log(`ui-module-in-sidebar=${sidebarHas}`);
  if (!sidebarHas) {
    await page.click('a:has-text("对话")');
    await page.waitForTimeout(800);
    const again = await page.locator('[data-testid="plugin-daily-quote"]').count();
    console.log(`ui-module-after-nav=${again}`);
    if (!again) throw new Error("UI 模块未在侧栏渲染");
  }
  await page.screenshot({ path: path.join(ROOT, "tests/local_market_ui_module.png"), fullPage: true });

  console.log("LOCAL MARKET OK");
  await browser.close();
})();
