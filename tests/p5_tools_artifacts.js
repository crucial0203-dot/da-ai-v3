/** p5 工具与制品：对话触发工具 → artifact 落库 → 侧栏预览 → 审批卡链路。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p5_tools_artifacts.js
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
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 30, note: "p5" }),
  }).then((r) => r.json());
  const code = inv.codes[0];

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(20000);

  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.fill("#code", code.toLowerCase());
  await page.fill("#nickname", "p5工具");
  await page.click("[data-testid=auth-submit]");
  await page.waitForSelector("[data-testid=composer-input]");

  // [1] 工具页渲染 + 真值表
  await page.click('a:text-is("工具")');
  await page.waitForSelector("[data-testid=tools-table]");
  const toolRows = await page.locator("[data-testid=tool-row]").count();
  check("工具页渲染", toolRows >= 5, `${toolRows} 行`);
  check("doc_generate 标需审批", await page.locator("[data-testid=tool-approval-badge]").first().isVisible());
  await page.locator("button:has-text('逻辑门真值表')").click();
  await page.waitForSelector("[data-testid=gate-tables]");
  check("真值表弹层", (await page.locator("[data-testid=gate-tables] table").count()) >= 7);
  await page.locator(".modal-actions .btn").click();

  // [2] 对话触发 artifact_create 工具（fake 上游吐工具块）
  await page.click('a:text-is("对话")');
  await page.waitForSelector("[data-testid=composer-input]");
  await page.fill("[data-testid=composer-input]",
    `#fake #tool {"tool_name":"artifact_create","tool_input":{"title":"p5测试图表","html":"<html><body><h1>p5 artifact</h1></body></html>"}} 做个图表`);
  await page.click("[data-testid=send-btn]");
  await page.waitForSelector("[data-testid=tool-tray]", { timeout: 15000 }).catch(() => {});
  const toolCardVisible = await page.locator("[data-testid=tool-card]").count();
  check("工具卡出现", toolCardVisible >= 1, `${toolCardVisible} 张`);

  // 等流结束（reload 后看侧栏 artifacts）
  await page.waitForTimeout(2500);
  const artRows = await page.locator("[data-testid=artifact-row]").count();
  check("侧栏 Artifacts 列表出现", artRows >= 1, `${artRows} 件`);

  if (artRows >= 1) {
    await page.locator("[data-testid=artifact-toggle]").first().click();
    const frame = page.locator(".artifact-frame");
    check("iframe 预览挂载", await frame.count() === 1);
    // 沙箱：无 allow-same-origin
    const sandbox = await frame.getAttribute("sandbox");
    check("iframe 无 same-origin（安全）", sandbox === "", `sandbox="${sandbox}"`);
  }

  // [3] 审批卡链路：doc_generate 需批准 → 假续跑
  await page.click('a:text-is("对话")');
  const taskId = (await page.evaluate(() => location.pathname)) || "";
  // 新建一个任务走审批
  await page.fill("[data-testid=composer-input]",
    `#fake #tool {"tool_name":"doc_generate","tool_input":{"doc_type":"docx","goal":"写一份行政协议读书笔记"}} 生成文档`);
  await page.click("[data-testid=send-btn]");
  const approvalCard = page.locator("[data-testid=tool-approval-card]");
  try {
    await approvalCard.waitFor({ timeout: 15000 });
    check("审批卡出现", true);
    check("审批卡显示工具名", (await approvalCard.locator(".approval-title").innerText()).includes("doc_generate"));
    // 拒绝（比批准快：批准会真跑 doc_generate）
    await page.click("[data-testid=tool-reject-btn]");
    // 卡片消失要等 POST 返回：续跑会再装配一次上下文（含联网检索），
    // 实测约 3s，固定时长的 waitForTimeout 会把 Race 测成 Bug——
    // 用条件等待（同 p10 ⑥ 的口径）。
    await page.waitForFunction(
      () => !document.querySelector("[data-testid=tool-approval-card]"),
      { timeout: 20000 },
    );
    check("拒绝后审批卡消失", true);
  } catch {
    check("审批卡出现", false, "15s 内没等到");
  }

  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p5_tools.png"), fullPage: true });
  await browser.close();
  console.log(`\n== p5: ${PASS.length} 过 / ${FAIL.length} 败 ==`);
  if (FAIL.length) { FAIL.forEach((f) => console.log("  - " + f)); process.exit(1); }
}

main().catch((err) => { console.error(err); process.exit(2); });
