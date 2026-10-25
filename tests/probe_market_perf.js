// 探针：市场 218 卡的真实渲染代价。只读测量，不改产品。
// 跑法：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/probe_market_perf.js
const { chromium } = require("playwright-core");
const fs = require("fs"), path = require("path");
const ROOT = "D:/workbuddyGZ/da-ai-v3";
const BASE = "http://127.0.0.1:8020";

function env(k) {
  return (fs.readFileSync(path.join(ROOT, ".env"), "utf8").split(/\r?\n/)
    .find((l) => l.startsWith(k + "=")) || "").slice(k.length + 1).trim();
}

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(30000);
  await page.goto(BASE, { waitUntil: "networkidle" });
  // 本地无 WAF：页内直接管理员登录 + 建一个邀请码
  const code = await page.evaluate(async (pw) => {
    await fetch("/api/admin/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pw }),
    });
    const r = await fetch("/api/admin/invites", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ count: 1, max_uses: 1, days: 1, daily_quota: 50, note: "perf探针" }),
    });
    const j = await r.json();
    return (j.codes || [""])[0];
  }, env("ADMIN_PASSWORD"));
  if (!code) { console.error("PROBE FAIL 建不了邀请码"); process.exit(1); }
  await page.fill("#code", code);
  await page.fill("#nickname", "perf探针");
  await page.click("[data-testid=auth-submit]").catch(() => {});
  await page.waitForSelector("[data-testid=composer-input]", { timeout: 15000 }).catch(() => {});

  await page.click('a[href="/plugins"]');
  await page.waitForSelector('[data-testid="plugins-page"]');

  // 切到 DSH Tab（200 卡）
  const t0 = Date.now();
  await page.click('[data-testid="market-tab-dsh"]');
  await page.waitForSelector('[data-testid="dsh-card"]');
  await page.waitForTimeout(300);
  const switchMs = Date.now() - t0;

  const stats = await page.evaluate(() => {
    const cards = Array.from(document.querySelectorAll("[data-testid=dsh-card]"));
    const grid = document.querySelector(".market-grid");
    return {
      cards: cards.length,
      totalNodes: document.querySelectorAll("*").length,
      gridScrollHeight: grid ? grid.scrollHeight : 0,
      firstCardTop: cards.length ? Math.round(cards[0].getBoundingClientRect().top) : -1,
      lastCardBottom: cards.length ? Math.round(cards[cards.length - 1].getBoundingClientRect().bottom) : -1,
    };
  });

  // 滚动帧率：从顶部匀速滚到底，统计长帧（>50ms）
  const frames = await page.evaluate(async () => {
    const scroller = document.scrollingElement || document.documentElement;
    const long = []; let last = performance.now(); let raf = true;
    const tick = () => { const now = performance.now(); if (now - last > 50) long.push(Math.round(now - last)); last = now; if (raf) requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
    const total = scroller.scrollHeight - scroller.clientHeight;
    const steps = 30;
    for (let i = 1; i <= steps; i++) {
      scroller.scrollTop = (total * i) / steps;
      await new Promise((r) => setTimeout(r, 32));
    }
    raf = false;
    return long;
  });

  console.log(JSON.stringify({ switchMs, ...stats, longFrames: frames.length, worstFrameMs: Math.max(0, ...frames) }, null, 1));
  await browser.close();
})().catch((e) => { console.error("PROBE FAIL", e.message); process.exit(1); });
