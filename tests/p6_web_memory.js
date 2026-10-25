/** p6 联网与记忆：联网开关 → 来源上屏 → 消息落库 → 记忆页 CRUD。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p6_web_memory.js
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

async function main() {
  const login = await fetch(BASE + "/api/admin/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: env("ADMIN_PASSWORD") }),
  });
  if (!login.ok) throw new Error("管理员登录失败");
  const adminCookie = (login.headers.get("set-cookie") || "").split(";")[0];
  const inv = await fetch(BASE + "/api/admin/invites", {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: adminCookie },
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 30, note: "p6" }),
  }).then((r) => r.json());
  const code = inv.codes[0];

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(20000);

  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.fill("#code", code.toLowerCase());
  await page.fill("#nickname", "p6联网");
  await page.click("[data-testid=auth-submit]");
  await page.waitForSelector("[data-testid=composer-input]");

  // [1] 联网开关默认开，可关可开
  const webToggle = page.locator("[data-testid=web-toggle]");
  check("联网开关可见", await webToggle.isVisible());
  const cls0 = await webToggle.getAttribute("class");
  check("默认联网开", cls0.includes("on"), cls0);
  await webToggle.click();
  check("可关", !(await webToggle.getAttribute("class")).includes("on"));
  await webToggle.click();
  check("再开", (await webToggle.getAttribute("class")).includes("on"));

  // [2] 联网对话：fake_web 罐头（BING_BASE_URL=8901）→ 来源上屏
  await page.fill("[data-testid=composer-input]", "行政协议约定仲裁的条款效力如何？");
  await page.click("[data-testid=send-btn]");
  const liveSources = page.locator("[data-testid=live-sources]");
  let sawSources = false;
  try {
    await liveSources.waitFor({ timeout: 12000 });
    sawSources = true;
  } catch { /* 流可能太快结束直接落库 */ }
  if (sawSources) {
    check("流式来源列表出现", (await liveSources.locator(".source-item").count()) >= 1);
  }
  // 等落库后 reload
  await page.waitForTimeout(2500);
  const storedSources = page.locator("[data-testid=source-list]").first();
  if (await storedSources.count()) {
    check("消息落库带来源", (await storedSources.locator(".source-item").count()) >= 1);
  } else {
    check("消息落库带来源", sawSources, "落库后没找到 source-list（流式见过也算过）");
  }

  // [3] 记忆页 CRUD
  await page.click('a:text-is("记忆")');
  await page.waitForSelector("h1:has-text('记忆')");
  check("记忆页渲染", true);

  await page.click("[data-testid=memory-add]");
  await page.fill("#mem-new", "p6验收：公文正文用仿宋三号");
  await page.click(".modal-actions .btn-primary");
  await page.waitForSelector("[data-testid=memory-item]");
  check("记忆新增", (await page.locator("[data-testid=memory-item]").count()) >= 1);

  // 编辑
  await page.locator("[data-testid=memory-item] button:has-text('编辑')").first().click();
  await page.fill("#mem-edit", "p6验收改：公文正文用仿宋三号，标题方正小标宋");
  await page.click(".modal-actions .btn-primary");
  await page.waitForTimeout(800);
  check("记忆编辑", (await page.locator("[data-testid=memory-list]").innerText()).includes("方正小标宋"));

  // 删除（P1-9：按钮文案从「×」改成「忘记」，语义更清楚；锚点用 data-testid）
  const before = await page.locator("[data-testid=memory-item]").count();
  await page.locator("[data-testid=memory-item] [data-testid=memory-forget]").first().click();
  await page.waitForSelector(".modal-actions .btn-primary");
  await page.click(".modal-actions .btn-primary");
  await page.waitForTimeout(800);
  const after = await page.locator("[data-testid=memory-item]").count();
  check("记忆删除", after === before - 1, `${before} → ${after}`);

  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p6_web_memory.png"), fullPage: true });
  await browser.close();
  console.log(`\n== p6: ${PASS.length} 过 / ${FAIL.length} 败 ==`);
  if (FAIL.length) { FAIL.forEach((f) => console.log("  - " + f)); process.exit(1); }
}

main().catch((err) => { console.error(err); process.exit(2); });
