/** p15 验收：本轮巡检修复的前端项（A5-A6 + B4-B7）。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p15_fixes.js
 * 前置：8020 已起且前端已 build；连续跑测试注意进站 429（脚本间 sleep 65-70s）。
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

async function newInvite(note) {
  const login = await fetch(BASE + "/api/admin/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: env("ADMIN_PASSWORD") }),
  });
  const cookie = (login.headers.get("set-cookie") || "").split(";")[0];
  const inv = await fetch(BASE + "/api/admin/invites", {
    method: "POST", headers: { "Content-Type": "application/json", Cookie: cookie },
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 30, note }),
  }).then((r) => r.json());
  return inv.codes[0];
}

async function main() {
  const code = await newInvite("p15");
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  page.setDefaultTimeout(25000);
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.fill("#code", code.toLowerCase());
  await page.fill("#nickname", "p15修复");
  await page.click("[data-testid=auth-submit]");
  await page.waitForSelector("[data-testid=composer-input]");

  // ---------- A5：输入法组合期回车不得发送 ----------
  await page.fill("[data-testid=composer-input]", "nihao");
  await page.evaluate(() => {
    const el = document.querySelector("[data-testid=composer-input]");
    el.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
    const ev = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    // 组合期内浏览器给的事件带 isComposing
    Object.defineProperty(ev, "isComposing", { get: () => true });
    el.dispatchEvent(ev);
  });
  await sleep(900);
  const stillTyped = await page.inputValue("[data-testid=composer-input]");
  const sent = await page.evaluate(async () => {
    const r = await fetch("/api/tasks?limit=20");
    const j = await r.json();
    return (j.tasks || []).some((t) => (t.goal || "").includes("nihao"));
  });
  check("[1] IME 组合期回车不发送也不断流", stillTyped === "nihao" && !sent,
    `输入框=${JSON.stringify(stillTyped)} 已发出=${sent}`);

  // 非组合期回车应当照常发送（防误伤）
  await page.fill("[data-testid=composer-input]", "这是一条正常提问");
  await page.keyboard.press("Enter");
  await sleep(1200);
  const sentNormal = await page.evaluate(async () => {
    const r = await fetch("/api/tasks?limit=20");
    const j = await r.json();
    return (j.tasks || []).some((t) => (t.goal || "").includes("正常提问"));
  });
  check("[2] 非组合期回车照常发送", sentNormal, `tasks=${sentNormal}`);

  // ---------- A6：切会话不串台 ----------
  // 需要至少两个会话：用接口先建出来，保证用例可重复
  const made = await page.evaluate(async () => {
    const ids = [];
    for (const goal of ["串台测试甲", "串台测试乙"]) {
      const r = await fetch("/api/tasks", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "research", goal }),
      });
      const j = await r.json();
      ids.push(j.id || (j.task && j.task.id));
    }
    return ids;
  });
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.waitForSelector("[data-testid=history-list]");
  const hrefs = await page.locator("[data-testid=history-list] a[href^='/t/']")
    .evaluateAll((els) => els.map((e) => e.getAttribute("href")));
  const pair = hrefs.length >= 2 ? hrefs : [`/t/${made[0]}`, `/t/${made[1]}`];

  if (pair.length >= 2) {
    await page.goto(BASE + pair[0], { waitUntil: "networkidle" });
    await page.waitForSelector("[data-testid=composer-input]");
    // 在 A 里发一条，让它进入流式
    await page.fill("[data-testid=composer-input]", "甲任务的正文内容XYZ");
    await page.keyboard.press("Enter");
    await sleep(1200);
    // 不等它结束就切到 B：走侧栏链接是 SPA 内跳转，组件实例被复用（串台的真实前提）
    const linkB = page.locator(`[data-testid=history-list] a[href='${pair[1]}']`).first();
    if (await linkB.count()) await linkB.click();
    else await page.goto(BASE + pair[1], { waitUntil: "networkidle" });
    await page.waitForSelector("[data-testid=composer-input]");
    await sleep(1500);
    const bText = await page.locator("body").innerText();
    check("[3] 切到 B 会话后不含 A 的正文",
      !bText.includes("甲任务的正文内容XYZ"), bText.includes("甲任务的正文内容XYZ") ? "B 页出现了 A 的片段" : "");
    const canType = await page.isEnabled("[data-testid=composer-input]");
    check("[4] 切会话后输入框可用（不再卡生成中）", canType);
  } else {
    check("[3] 切到 B 会话后不含 A 的正文", false, `可用会话=${pair.length}`);
    check("[4] 切会话后输入框可用", false, "同上");
  }

  // ---------- B4：主题换肤真的生效 ----------
  await page.click('a[href="/plugins"]');
  await page.waitForSelector("[data-testid=plugins-page]");
  await page.click("[data-testid=market-tab-themes]");
  await page.waitForSelector("[data-testid=plugin-card]");
  await sleep(400);
  const basePrimary = await page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue("--primary").trim());
  await page.locator("[data-testid=market-install-btn]").first().click();
  await page.waitForSelector("[data-testid=market-confirm-ok]");
  await page.click("[data-testid=market-confirm-ok]");
  await sleep(1200);
  const themed = await page.evaluate(() => ({
    primary: getComputedStyle(document.documentElement).getPropertyValue("--primary").trim(),
    attr: document.documentElement.getAttribute("data-market-theme"),
    ls: localStorage.getItem("da_market_theme"),
  }));
  check("[5] 启用主题后 CSS 变量真的落地（不被自己抹掉）",
    themed.primary !== "" && themed.primary !== basePrimary,
    `base=${basePrimary || "''"} → themed=${themed.primary} attr=${themed.attr}`);
  check("[6] 主题标记与 localStorage 一致",
    Boolean(themed.attr) && themed.ls === themed.attr, `attr=${themed.attr} ls=${themed.ls}`);
  // 清理，避免影响后续用例
  await page.evaluate(async () => {
    await fetch("/api/market/uninstall", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: localStorage.getItem("da_market_theme") }),
    });
  });

  // ---------- B7：上一轮残留的 tick 占位符 ----------
  const bodyText = await page.locator("body").innerText();
  check("[7] 页面不再渲染调试残留「tick」",
    !/（tick|（tick.*?）|tick\s*）/i.test(bodyText),
    bodyText.includes("tick") ? bodyText.slice(bodyText.indexOf("tick") - 20, bodyText.indexOf("tick") + 30) : "");

  // ---------- B6：已安装 Tab 不给浏览型条目渲染安装按钮 ----------
  await page.route("**/api/market/catalog", async (route) => {
    const resp = await route.fetch();
    const json = await resp.json();
    const dshItem = (json.plugins || []).find((p) => p.kind === "dsh-ref");
    if (dshItem) dshItem.installed = true;
    await route.fulfill({ response: resp, json });
  });
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForSelector("[data-testid=plugins-page]");
  await page.click("[data-testid=market-tab-installed]");
  await sleep(900);
  const installing = await page.locator("[data-testid=market-install-btn]").count();
  const uninstalling = await page.locator("[data-testid=market-uninstall-btn]").count();
  const dshCards = await page.locator("[data-testid=dsh-card]").count();
  check("[8] 已安装 Tab 中浏览型条目没有安装/卸载按钮",
    dshCards > 0 && installing === 0 && uninstalling === 0,
    `dsh卡=${dshCards} 安装=${installing} 卸载=${uninstalling}`);
  await page.unroute("**/api/market/catalog");

  // ---------- B5：窄屏保留生成链路 ----------
  await page.setViewportSize({ width: 900, height: 900 });
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  const refs2 = await page.locator('a[href^="/t/"]').evaluateAll((els) =>
    els.map((e) => e.getAttribute("href")).filter(Boolean));
  if (refs2.length) {
    await page.goto(BASE + refs2[0], { waitUntil: "networkidle" });
    await page.waitForSelector("[data-testid=composer-input]");
    await sleep(500);
    const gen = page.locator("[data-testid=generate-btn]");
    const visible = await gen.isVisible().catch(() => false);
    check("[9] 900px 窄屏仍能看到「生成文件」按钮", visible);
    const asideVisible = await page.locator(".task-aside").isVisible().catch(() => false);
    check("[10] 900px 下右侧材料区改为重排而非隐藏", asideVisible);
  } else {
    check("[9] 900px 窄屏仍能看到「生成文件」按钮", false, "没有任务页可进");
    check("[10] 900px 下右侧材料区改为重排而非隐藏", false, "同上");
  }

  await page.setViewportSize({ width: 1440, height: 900 });
  await browser.close();
  console.log();
  console.log("=".repeat(46));
  console.log(`通过 ${PASS.length}/${PASS.length + FAIL.length}`);
  if (FAIL.length) console.log("失败项：", FAIL.join(" | "));
  process.exit(FAIL.length ? 1 : 0);
}

main().catch((e) => { console.error("P15 ERROR", e.message); process.exit(2); });
