// 线上冒烟：进站 → 市场 218 条/7 Tab/DSH 渲染 → 主题装卸回归 → 乱源专项 → 命令面板
const { chromium } = require("playwright-core");
const path = require("path");
const ROOT = "D:/workbuddyGZ/da-ai-v3";
const BASE = "https://da-ai.app.workbuddy.host/?sharecode=2GAE2PHAAJ";
const ORIGIN = "https://da-ai.app.workbuddy.host";
const SHARECODE = "2GAE2PHAAJ";
const CODE = process.argv[2] || "7CAM8SNE";

let pass = 0, fail = 0;
function check(name, ok, detail) {
  if (ok) { pass++; console.log(`  PASS ${name}`); }
  else { fail++; console.log(`  FAIL ${name} :: ${detail || ""}`); }
}

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(30000);

  // S1 进站登录
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 60000 });
  if (await page.locator("[data-testid=auth-form]").isVisible().catch(() => false)) {
    await page.fill("#code", CODE);
    await page.fill("#nickname", "p14线上冒烟");
    await page.click("[data-testid=auth-submit]");
  }
  await page.waitForSelector("[data-testid=composer-input]");
  check("S1 进站登录", true);

  // S2 目录 218 条含 dsh-ref
  const cat = await page.evaluate(async () => {
    const r = await fetch("/api/market/catalog");
    const j = await r.json();
    const kinds = {};
    for (const p of j.plugins || []) kinds[p.kind] = (kinds[p.kind] || 0) + 1;
    return { status: r.status, count: j.count, kinds };
  });
  check("S2 目录 218 条", cat.status === 200 && cat.count === 218, JSON.stringify(cat));
  check("S2 dsh-ref=200", cat.kinds["dsh-ref"] === 200, JSON.stringify(cat.kinds));

  // S3 市场页 7 Tab + DSH 渲染
  const respP = page.waitForResponse((r) => r.url().includes("/api/market/catalog"), { timeout: 30000 }).catch(() => null);
  await page.click('a[href="/plugins"]');
  await page.waitForSelector('[data-testid="plugins-page"]');
  await respP;
  const tabCount = await page.locator("[data-testid^=market-tab-]").count();
  check("S3 7 个 Tab", tabCount === 7, `tabCount=${tabCount}`);

  await page.click('[data-testid="market-tab-dsh"]');
  await page.waitForSelector('[data-testid="dsh-card"]');
  await page.waitForTimeout(600);
  const dshCards = await page.locator('[data-testid="dsh-card"]').count();
  check("S3 DSH 200 卡片", dshCards === 200, `dshCards=${dshCards}`);

  // chips 只含 DSH 分类
  const chips = await page.evaluate(() =>
    Array.from(document.querySelectorAll("[data-testid^=market-cat-dsh-]")).map((el) => el.textContent.trim())
  );
  const badChips = chips.filter((c) => c && !c.startsWith("DSH"));
  check("S3 chips 无跨族混入", badChips.length === 0, JSON.stringify(chips));

  // 排序菜单存在且非全宽
  const sortBox = await page.locator(".market-sort").boundingBox();
  check("S3 排序菜单宽度正常", sortBox && sortBox.width < 400, JSON.stringify(sortBox));

  // S4 DSH 卡外链 + 不可安装
  const first = page.locator('[data-testid="dsh-card"]').first();
  const ghHref = await first.locator('a[data-testid="dsh-github-link"]').first().getAttribute("href").catch(() => null);
  check("S4 去 GitHub 外链", typeof ghHref === "string" && /^https:\/\/(github\.com|www\.npmjs\.com)\//.test(ghHref), String(ghHref));
  const installBtns = await page.locator('[data-testid="dsh-card"] [data-testid="market-install-btn"]').count();
  check("S4 DSH 无安装按钮", installBtns === 0, `installBtns=${installBtns}`);

  // S5 原有插件装/卸回归（深空蓝主题）
  await page.click('[data-testid="market-tab-themes"]');
  await page.waitForTimeout(400);
  // 先卸载保证可重复
  await page.evaluate(async () => {
    await fetch("/api/market/uninstall", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: "theme.deep-space" }) }).catch(() => undefined);
  });
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForSelector('[data-testid="plugin-card"]');
  await page.click('[data-testid="market-tab-themes"]');
  await page.waitForTimeout(300);
  const deepSpace = page.locator('[data-testid="plugin-card"]', { hasText: "深空蓝" });
  await deepSpace.locator('[data-testid="market-install-btn"]').click();
  await page.waitForSelector('[data-testid="market-confirm-ok"]');
  await page.click('[data-testid="market-confirm-ok"]');
  await page.waitForFunction(() => document.documentElement.getAttribute("data-market-theme") === "theme.deep-space", null, { timeout: 20000 });
  check("S5 主题安装换肤", true);
  await page.evaluate(async () => {
    await fetch("/api/market/uninstall", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: "theme.deep-space" }) });
  });
  // 直接调 API 卸载绕过了前端 store，重载页面让主题状态归位后再断言
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForFunction(() => document.documentElement.getAttribute("data-market-theme") !== "theme.deep-space", null, { timeout: 15000 });
  check("S5 主题卸载还原", true);

  // S6 乱源专项：clean_query 洗出「做PPT」继续搜 → 来源应为 PPT 相关而非单字「你」
  const task = await page.evaluate(async () => {
    const r = await fetch("/api/tasks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: "research", goal: "冒烟-乱源专项" }) });
    return r.json();
  });
  const tid = task.id || (task.task && task.task.id);
  check("S6 建任务", !!tid, JSON.stringify(task).slice(0, 120));

  const sseData = await page.evaluate(async (tid) => {
    const r = await fetch(`/api/tasks/${tid}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: "你能不能做PPT" }),
    });
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "", events = [], searchSources = null, done = false;
    const t0 = Date.now();
    while (!done && Date.now() - t0 < 240000) {
      const { value, done: d } = await reader.read();
      if (d) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const em = chunk.match(/^event: (.+)$/m);
        const dm = chunk.match(/^data: (.+)$/m);
        if (em) {
          events.push(em[1]);
          if (em[1] === "search" && dm) {
            try { searchSources = JSON.parse(dm[1]).sources || []; } catch { searchSources = []; }
          }
        }
        if (em && em[1] === "done") done = true;
        if (dm && dm[1] === "[DONE]") done = true;
      }
    }
    return { events, searchSources };
  }, tid);
  const ssrc = sseData.searchSources || [];
  const titles = ssrc.map((s) => s.title || "");
  const relevant = titles.filter((t) => /ppt|PPT|演示|幻灯|powerpoint|slide|报告|制作/i.test(t)).length;
  const junk = titles.filter((t) => /^(你)( |$|的|要|能)/.test(t) && !/ppt/i.test(t)).length;
  check("S6 有 search 事件且来源非空", sseData.events.includes("search") && ssrc.length > 0,
    `events=${JSON.stringify(sseData.events.slice(0, 8))}`);
  check("S6 来源是 PPT 相关（非单字「你」乱源）", relevant >= Math.ceil(titles.length / 2) && junk === 0,
    `titles=${JSON.stringify(titles)} relevant=${relevant} junk=${junk}`);

  // S7 命令面板
  await page.click('a[href="/"]');
  await page.waitForSelector("[data-testid=composer-input]");
  await page.keyboard.press("Control+k");
  await page.waitForSelector('[data-testid="command-palette"]', { timeout: 8000 }).catch(() => null);
  const paletteOpen = await page.locator('[data-testid="command-palette"]').isVisible().catch(() => false);
  check("S7 命令面板 mod+k", paletteOpen);
  await page.keyboard.press("Escape");

  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p14_online_smoke.png") });
  // S8 本轮修复的三项线上专项
  // S8-1 输入法组合期回车不发送
  await page.click('a[href="/"]').catch(() => null);
  await page.waitForSelector("[data-testid=composer-input]");
  await page.fill("[data-testid=composer-input]", "nihao");
  await page.evaluate(() => {
    const el = document.querySelector("[data-testid=composer-input]");
    el.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
    const ev = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    Object.defineProperty(ev, "isComposing", { get: () => true });
    el.dispatchEvent(ev);
  });
  await page.waitForTimeout(900);
  check("S8-1 IME 组合期回车不发送",
    (await page.inputValue("[data-testid=composer-input]")) === "nihao",
    JSON.stringify(await page.inputValue("[data-testid=composer-input]")));

  // S8-2 主题换肤真的生效（CSS 变量不被互斥清理抹掉）
  await page.click('a[href="/plugins"]');
  await page.waitForSelector("[data-testid=plugins-page]");
  await page.click("[data-testid=market-tab-themes]");
  await page.waitForSelector("[data-testid=plugin-card]");
  await page.waitForTimeout(400);
  const basePrimary = await page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue("--primary").trim());
  await page.locator("[data-testid=market-install-btn]").first().click();
  await page.waitForSelector("[data-testid=market-confirm-ok]");
  await page.click("[data-testid=market-confirm-ok]");
  await page.waitForTimeout(1500);
  const themed = await page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue("--primary").trim());
  check("S8-2 启用主题后 CSS 变量真的变化",
    themed !== "" && themed !== basePrimary, `base=${basePrimary || "''"} → ${themed}`);
  await page.evaluate(async () => {
    await fetch("/api/market/uninstall", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: localStorage.getItem("da_market_theme") }),
    });
  });

  // S8-3 窄屏仍能看到「生成文件」按钮
  await page.setViewportSize({ width: 900, height: 900 });
  await page.goto(ORIGIN + "/?sharecode=" + SHARECODE, { waitUntil: "networkidle" });
  await page.waitForSelector("[data-testid=composer-input]");
  const tRefs = await page.locator('a[href^="/t/"]').evaluateAll((els) =>
    els.map((e) => e.getAttribute("href")).filter(Boolean));
  if (tRefs.length) {
    await page.goto(`${ORIGIN}${tRefs[0]}?sharecode=${SHARECODE}`, { waitUntil: "networkidle" });
    await page.waitForSelector("[data-testid=composer-input]");
    await page.waitForTimeout(700);
    check("S8-3 900px 窄屏仍可见「生成文件」",
      await page.locator("[data-testid=generate-btn]").isVisible().catch(() => false));
  } else {
    check("S8-3 900px 窄屏仍可见「生成文件」", false, "线上没有可用会话");
  }

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p14_online_smoke.png") });
  await browser.close();
  console.log(`\nRESULT pass=${pass} fail=${fail}`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error("SMOKE ERROR", e.message); process.exit(2); });
