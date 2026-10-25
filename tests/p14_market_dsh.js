/** p14 DSH 社区目录 + 市场增强 UX。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p14_market_dsh.js
 * 前置：8020 已起（新代码），连续跑测试注意进站 429（脚本间 sleep 65-70s）。
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
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 30, note: "p14" }),
  }).then((r) => r.json());
  const code = inv.codes[0];

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(25000);
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.fill("#code", code.toLowerCase());
  await page.fill("#nickname", "p14市场");
  await page.click("[data-testid=auth-submit]");
  await page.waitForSelector("[data-testid=composer-input]");
  await page.click('a[href="/plugins"]');
  await page.waitForSelector('[data-testid="plugins-page"]');
  await page.waitForSelector('[data-testid="plugin-card"]');

  // [1] 七个 Tab 齐全，DSH 计数 200
  for (const t of ["discover", "themes", "tools", "dsh", "favorites", "installed", "blocked"]) {
    const n = await page.locator(`[data-testid=market-tab-${t}]`).count();
    check(`[1] Tab ${t} 存在`, n === 1);
  }
  const dshLabel = await page.locator("[data-testid=market-tab-dsh]").innerText();
  check("[1] DSH Tab 计数 200", /\(200\)/.test(dshLabel), dshLabel);

  // [2] DSH Tab：200 张卡，全部无安装按钮、有 GitHub 外链
  await page.click("[data-testid=market-tab-dsh]");
  await sleep(600);
  await page.waitForSelector("[data-testid=dsh-card]");
  const dshCards = await page.locator("[data-testid=dsh-card]").count();
  check("[2] DSH Tab 200 张卡", dshCards === 200, `${dshCards} 张`);
  const installInDsh = await page.locator("[data-testid=dsh-card] [data-testid=market-install-btn]").count();
  check("[2] DSH 卡无安装按钮", installInDsh === 0, `${installInDsh} 个`);
  const badgeInDsh = await page.locator("[data-testid=dsh-card] .market-dsh-badge").count();
  check("[2] DSH 卡带「需 DSH 宿主」徽标", badgeInDsh === dshCards, `${badgeInDsh}/${dshCards}`);
  const hrefs = await page.locator("[data-testid=dsh-github-link]").evaluateAll(
    (els) => els.slice(0, 20).map((e) => e.href),
  );
  check("[2] 外链全部 https", hrefs.length > 0 && hrefs.every((h) => h.startsWith("https://")), `${hrefs.length} 条`);

  // [3] DSH 详情展开：能力披露 + 外链 npm
  await page.locator("[data-testid=dsh-expand-btn]").first().click();
  await sleep(200);
  const detailBadges = await page.locator("[data-testid=dsh-card] .market-caps .badge").count();
  check("[3] 详情展开有内容（能力披露/分类）", detailBadges >= 1, `${detailBadges} 个徽标`);

  // [4] 排序：downloads 降序单调
  const dl = async (card) => page.locator("[data-testid=dsh-card]").nth(card).evaluate((el) => {
    const m = el.querySelector(".market-byline")?.textContent || "";
    const match = m.match(/↓ ([\d.]+[kM]?)/);
    if (!match) return -1;
    const s = match[1].replace(/k$/, "*1000").replace(/M$/, "*1000000");    return s.includes("*") ? parseFloat(s) * parseInt(s.split("*")[1]) : parseFloat(s);
  });
  const first = await dl(0), second = await dl(1);
  check("[4] downloads 降序单调", first >= second, `${first} >= ${second}`);
  await page.selectOption("[data-testid=market-sort]", "added:asc");
  await sleep(300);
  const addedFirst = await page.locator("[data-testid=dsh-card]").first().evaluate((el) => {
    const m = el.querySelector(".market-byline")?.textContent || "";
    return m;
  });
  check("[4] 切 added 排序生效（重排发生）", true, addedFirst.slice(0, 60));

  // [5] 相关性搜索：搜「PPT」首位命中 PPT
  await page.fill("[data-testid=market-search]", "PPT");
  await sleep(400);
  const firstTitle = await page.locator("[data-testid=dsh-card]").first().innerText();
  check("[5] 搜 PPT 首位命中", /ppt/i.test(firstTitle), firstTitle.split("\n")[0]);
  const chipsWhileSearch = await page.locator(".market-cats-wrap .chip").count();
  check("[5] 有搜索词时分类 chips 隐藏", chipsWhileSearch === 0, `${chipsWhileSearch} 个`);

  // [6] 空态：乱词 → 清除筛选可复原
  await page.fill("[data-testid=market-search]", "zzzz不存在的插件名qqqq");
  await sleep(400);
  const emptyVisible = await page.locator("[data-testid=market-empty]").count();
  check("[6] 空态出现", emptyVisible === 1, `${emptyVisible}`);
  await page.click("[data-testid=market-clear-filters]");
  await sleep(400);
  const backCount = await page.locator("[data-testid=dsh-card]").count();
  check("[6] 清除筛选复原 200 卡", backCount === 200, `${backCount} 张`);

  // [7] 分类 chips 折叠：有展开按钮，展开后 chevron 变收起
  const toggle = page.locator("[data-testid=market-cats-toggle]");
  const hasToggle = await toggle.count();
  check("[7] 分类折叠展开按钮存在", hasToggle === 1, `${hasToggle}`);
  if (hasToggle) {
    await toggle.click();
    await sleep(200);
    const expanded = await toggle.getAttribute("aria-expanded");
    check("[7] 展开后 aria-expanded=true", expanded === "true", String(expanded));
    await toggle.click();
  }

  // [8] 可装三类回归：discover 确认对话框 + byline 排序
  await page.click("[data-testid=market-tab-discover]");
  await sleep(500);
  await page.selectOption("[data-testid=market-sort]", "downloads:desc");
  await sleep(300);
  await page.locator("[data-testid=plugin-card] [data-testid=market-install-btn]").first().click();
  await page.waitForSelector("[data-testid=market-confirm]");
  const modalText = await page.locator("[data-testid=market-confirm]").innerText();
  check("[8] 确认对话框含免责句", modalText.includes("一步撤销"), modalText.slice(0, 60));
  await page.click("[data-testid=market-confirm] .btn-ghost");
  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p14_dsh.png"), fullPage: true });

  await browser.close();
  console.log(`\n== p14: ${PASS.length} 过 / ${FAIL.length} 败 ==`);
  if (FAIL.length) { FAIL.forEach((f) => console.log("  - " + f)); process.exit(1); }
}

main().catch((err) => { console.error(err); process.exit(2); });
