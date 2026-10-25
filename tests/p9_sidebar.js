/** p9 侧栏历史：去硬截断 + 增量渲染 + 筛选空态可清空。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p9_sidebar.js
 *
 * 断言：造 120 条 → 首屏 50 → 滚到底 100 → 再滚不超量 →
 *       筛选无结果出「清空筛选」→ 点它恢复全量。
 */
const { chromium } = require("playwright-core");
const fs = require("fs"), path = require("path");
const ROOT = "D:/workbuddyGZ/da-ai-v3";
const BASE = "http://127.0.0.1:8020";
const SEED = 120;

function env(k) {
  return (fs.readFileSync(path.join(ROOT, ".env"), "utf8").split(/\r?\n/)
    .find((l) => l.startsWith(k + "=")) || "").slice(k.length + 1).trim();
}
const PASS = [], FAIL = [];
function check(name, ok, detail = "") {
  (ok ? PASS : FAIL).push(name);
  console.log(`${ok ? "  OK " : "  !! "} ${name}${detail ? `  [${detail}]` : ""}`);
}

async function main() {
  // 准备邀请码
  const login = await fetch(BASE + "/api/admin/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: env("ADMIN_PASSWORD") }),
  });
  const adminCookie = (login.headers.get("set-cookie") || "").split(";")[0];
  const inv = await fetch(BASE + "/api/admin/invites", {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: adminCookie },
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 50, note: "p9" }),
  }).then((r) => r.json());
  const code = inv.codes[0];

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(30000);

  // [1] 进站
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.fill("#code", code.toLowerCase());
  await page.fill("#nickname", "p9侧栏");
  await page.click("[data-testid=auth-submit]");
  await page.waitForSelector("[data-testid=history-list]");

  // [2] 造 120 条会话（走浏览器上下文，自动带 cookie）
  let created = 0;
  for (let i = 0; i < SEED; i++) {
    const r = await page.request.post(BASE + "/api/tasks", {
      data: { goal: `p9 侧栏增量会话 ${i}`, mode: "chat", title: `p9-${i}` },
    });
    if (r.ok()) created++;
  }
  check(`造出 ${SEED} 条会话`, created === SEED, `created=${created}`);

  // [3] 重进页面让 store 重新拉列表
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForSelector("[data-testid=history-list]");
  await page.waitForFunction(
    () => document.querySelectorAll(".side-hist").length > 0,
    { timeout: 10000 },
  );

  // [4] 首屏 50（不是原来的硬截断 30，也不是一次全量 100）
  const first = await page.locator(".side-hist").count();
  check("首屏 50 条", first === 50, `count=${first}`);
  check("出现「还有更多」提示", (await page.locator("[data-testid=history-more]").count()) === 1);

  // [5] 滚到底 → 100
  const list = page.locator("[data-testid=history-list]");
  await list.evaluate((el) => {
    el.scrollTop = el.scrollHeight;
    el.dispatchEvent(new Event("scroll", { bubbles: true }));
  });
  await page.waitForFunction(
    () => document.querySelectorAll(".side-hist").length === 100,
    { timeout: 5000 },
  );
  check("滚到底加载到 100 条", true);

  // [6] 再滚一次不超量（store 只有 100，提示应消失）
  await list.evaluate((el) => {
    el.scrollTop = el.scrollHeight;
    el.dispatchEvent(new Event("scroll", { bubbles: true }));
  });
  await page.waitForTimeout(300);
  const afterSecond = await page.locator(".side-hist").count();
  check("再滚不超量", afterSecond === 100, `count=${afterSecond}`);
  check("加载完后提示消失", (await page.locator("[data-testid=history-more]").count()) === 0);

  // [7] 筛选无结果 → 空态 + 清空筛选 → 恢复
  await page.fill(".side-search input", "绝对不存在的关键词zzz");
  await page.waitForFunction(
    () => document.body.innerText.includes("没有匹配的会话"),
    { timeout: 5000 },
  );
  check("筛选空态文案", true);
  await page.click("[data-testid=history-clear-filter]");
  await page.waitForFunction(
    () => document.querySelectorAll(".side-hist").length === 50,
    { timeout: 5000 },
  );
  check("清空筛选后恢复 50 条", true);

  // [8] 后端确实有 120 条（证明前端真的只渲染了一部分）
  const total = await page.request.get(BASE + "/api/tasks?limit=100").then((r) => r.json());
  check("后端持有 >=100 条（上限封顶）", (total.tasks || []).length === 100, `len=${(total.tasks || []).length}`);

  await browser.close();
  console.log(`\n${FAIL.length ? "FAIL" : "PASS"}  ${PASS.length} ok / ${FAIL.length} failed`);
  process.exit(FAIL.length ? 1 : 0);
}

main().catch((err) => {
  console.error("p9 运行异常：", err);
  process.exit(1);
});
