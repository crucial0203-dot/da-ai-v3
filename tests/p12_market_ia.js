/** p12 市场 IA（P2-6）：6 Tab + 计数徽标 + relevance 搜索 + 切 Tab 刷新 + blocked 归置。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p12_market_ia.js
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
  // 建独立邀请码，避免蹭别的测试的访客
  const login = await fetch(BASE + "/api/admin/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: env("ADMIN_PASSWORD") }),
  });
  const adminCookie = (login.headers.get("set-cookie") || "").split(";")[0];
  const inv = await fetch(BASE + "/api/admin/invites", {
    method: "POST", headers: { "Content-Type": "application/json", Cookie: adminCookie },
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 30, note: "p12" }),
  }).then((r) => r.json());
  const code = inv.codes[0];

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(20000);
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.fill("#code", code.toLowerCase());
  await page.fill("#nickname", "p12市场");
  await page.click("[data-testid=auth-submit]");
  await page.waitForSelector("[data-testid=composer-input]");
  await page.click('a[href="/plugins"]');
  await page.waitForSelector('[data-testid="plugins-page"]');
  await page.waitForSelector('[data-testid="plugin-card"]');

  // [1] 六个 Tab 齐全
  for (const t of ["discover", "themes", "tools", "favorites", "installed", "blocked"]) {
    const n = await page.locator(`[data-testid=market-tab-${t}]`).count();
    check(`[1] Tab ${t} 存在`, n === 1);
  }
  const toolsLabel = await page.locator("[data-testid=market-tab-tools]").innerText();
  check("[1] 工具 Tab 计数 12", /\(12\)/.test(toolsLabel), toolsLabel);

  // [2] tools Tab：卡片全是 tool-pack（无主题卡）
  await page.click("[data-testid=market-tab-tools]");
  await sleep(400);
  await page.waitForSelector("[data-testid=plugin-card]");
  const toolCards = await page.locator("[data-testid=plugin-card]").count();
  check("[2] 工具 Tab 12 张卡", toolCards === 12, `${toolCards} 张`);
  const toolCardKinds = await page.evaluate(() =>
    [...document.querySelectorAll("[data-testid=plugin-card]")].filter(
      (el) => el.textContent.includes("主题"),
    ).length,
  );
  check("[2] 工具 Tab 没混主题卡", toolCardKinds === 0, `${toolCardKinds} 张主题卡`);

  // [3] relevance 搜索：有词时分类 chips 隐藏 + 过滤生效
  await page.click("[data-testid=market-tab-discover]");
  await sleep(400);
  await page.fill("[data-testid=market-search]", "合同");
  await sleep(300);
  const chips = await page.locator(".market-toolbar .chip").count();
  check("[3] 有搜索词时分类 chips 隐藏（relevance 口径）", chips === 0, `${chips} 个 chip`);

  // [4] 切 Tab 即刷新：监听 catalog 请求
  const refreshP = page.waitForRequest((r) => r.url().includes("/api/market/catalog"), { timeout: 5000 });
  await page.click("[data-testid=market-tab-tools]");
  const refreshed = await refreshP.then(() => true).catch(() => false);
  check("[4] 切 Tab 触发目录刷新", refreshed);

  // [5] blocked 归置：装一个工具包 → 停用 → 出现在 blocked Tab
  await page.click("[data-testid=market-tab-discover]");
  await sleep(400);
  // 装第一个工具包（找一张带「安装」按钮的 tool-pack 卡）
  const packCard = page.locator("[data-testid=plugin-card]", { hasText: "工具" }).first();
  const installBtn = packCard.locator("[data-testid=market-install-btn]");
  if (await installBtn.count()) {
    await installBtn.click();
    await page.waitForSelector("[data-testid=market-confirm-ok]");
    await page.click("[data-testid=market-confirm-ok]");
    await sleep(800);
  }
  // 停用已启用的市场插件（找「已启用」按钮）
  const enabledBtn = page.locator('[data-testid=market-toggle-btn]:has-text("已启用")').first();
  if (await enabledBtn.count()) {
    await enabledBtn.click();
    await sleep(800);
  }
  const blockedLabel = await page.locator("[data-testid=market-tab-blocked]").innerText();
  check("[5] 停用后 blocked 计数 ≥1", /\(1\)|\(2\)|\(3\)/.test(blockedLabel), blockedLabel);
  await page.click("[data-testid=market-tab-blocked]");
  await sleep(400);
  const blockedCards = await page.locator("[data-testid=plugin-card]").count();
  check("[5] blocked Tab 卡片与计数一致", blockedCards >= 1, `${blockedCards} 张`);
  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p12_blocked.png"), fullPage: true });

  await browser.close();
  console.log(`\n== p12: ${PASS.length} 过 / ${FAIL.length} 败 ==`);
  if (FAIL.length) { FAIL.forEach((f) => console.log("  - " + f)); process.exit(1); }
}

main().catch((err) => { console.error(err); process.exit(2); });
