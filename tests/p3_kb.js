/** p3 知识库：建集合 → 贴文字 → 检索命中 → 切块预览 → composer 选择器。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p3_kb.js
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
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 30, note: "p3" }),
  }).then((r) => r.json());
  const code = inv.codes[0];

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(15000);

  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.fill("#code", code.toLowerCase());
  await page.fill("#nickname", "p3知识库");
  await page.click("[data-testid=auth-submit]");
  await page.waitForSelector("[data-testid=composer-input]");

  // [1] 进知识库页
  await page.click('a:text-is("知识库")');
  await page.waitForSelector("h1:has-text('知识库')");
  check("知识库页渲染", true);

  // [2] 新建集合
  const colName = `p3集合${Date.now() % 100000}`;
  await page.click("[data-testid=kb-new]");
  await page.fill("#col-name", colName);
  await page.fill("#col-desc", "p3 验收用");
  await page.click(".modal-actions .btn-primary");
  await page.waitForSelector(`[data-testid=kb-item]:has-text("${colName}")`);
  check("集合创建成功", true);

  // [3] 贴文字入库
  await page.click("[data-testid=kb-add-text]");
  await page.fill("#doc-name", "行政协议仲裁条款");
  await page.fill("#doc-text",
    "行政协议案件中，一方当事人请求仲裁的，仲裁机构不予受理，但当事人约定仲裁的除外。" +
    "行政协议的效力审查应当优先适用行政法规范，民事法律规范可以补充适用。" +
    "行政机关不依法履行、未按照约定履行行政协议，人民法院可以判决行政机关继续履行，并明确继续履行的具体内容。".repeat(6));
  await page.click(".modal-actions .btn-primary");
  await page.waitForSelector("table.data-table");
  check("文档入库", await page.locator("table.data-table tbody tr").count() >= 1);

  // [4] 检索命中
  await page.fill("[data-testid=kb-search-input]", "行政协议 仲裁");
  await page.click("[data-testid=kb-search-btn]");
  await page.waitForSelector("[data-testid=kb-hits]");
  const hitCount = await page.locator("[data-testid=kb-hits] .kb-hit").count();
  check("检索有命中", hitCount >= 1, `${hitCount} 条`);

  // [5] 切块预览（容器先渲染、chunks 后到，必须等首条命中）
  await page.locator("table.data-table button:has-text('切块')").first().click();
  const chunkHit = await page
    .waitForSelector("[data-testid=kb-chunks] .kb-hit", { timeout: 8000 })
    .catch(() => null);
  check("切块预览渲染", !!chunkHit,
    chunkHit ? "" : `容器=${await page.locator("[data-testid=kb-chunks]").count()} 条目=${await page.locator("[data-testid=kb-chunks] .kb-hit").count()}`);

  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p3_kb.png"), fullPage: true });

  // [6] composer 出知识库选择器（集合非空才显示）
  await page.click('a:text-is("对话")');
  await page.waitForSelector("[data-testid=composer-input]");
  // 集合列表是 composer 挂载后异步拉的，等按钮真正出现
  await page.waitForSelector("[data-testid=rag-collection]", { timeout: 8000 }).catch(() => null);
  const ragBtn = page.locator("[data-testid=rag-collection]");
  check("composer 出知识库按钮", await ragBtn.isVisible());
  if (await ragBtn.isVisible()) {
    await ragBtn.click();
    await page.waitForSelector("[data-testid=rag-pop]");
    check("知识库浮层可开", true);
    await page.locator("[data-testid=rag-pop] .skill-opt").first().click();
    check("可勾选集合", (await page.locator("[data-testid=rag-collection]").innerText()).includes("1"));
  }

  // [7] 联网开关存在（meta.web.enabled 时）
  check("composer 出联网开关", await page.locator("[data-testid=web-toggle]").isVisible());

  await browser.close();
  console.log(`\n== p3: ${PASS.length} 过 / ${FAIL.length} 败 ==`);
  if (FAIL.length) { FAIL.forEach((f) => console.log("  - " + f)); process.exit(1); }
}

main().catch((err) => { console.error(err); process.exit(2); });
