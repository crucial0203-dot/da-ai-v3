/** p8 流式合帧验收：rAF batcher 压缩比 + 顺序完整 + 尾帧不丢。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p8_stream_perf.js
 * 前提：本地服务以 DEBUG_MODE=1 FAKE_UPSTREAM=1 起在 :8020（#fake 假上游）。
 *
 * 断言（对应 streamBuffer.ts 三条硬契约）：
 *   ① flushes >= 1                     合帧器确实在工作
 *   ② flushes <= received / 5          至少 5 倍压缩（逐 token 重渲染是原病灶）
 *   ③ 持久化正文 == 200 段按序拼接     保序 + 完整 + 尾帧没被 dispose 丢掉
 */
const { chromium } = require("playwright-core");
const fs = require("fs"), path = require("path");
const ROOT = "D:/workbuddyGZ/da-ai-v3";
const BASE = "http://127.0.0.1:8020";
const CHUNKS = 200;

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
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 50, note: "p8" }),
  }).then((r) => r.json());
  const code = inv.codes[0];

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(30000);

  // [1] 进站
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.fill("#code", code.toLowerCase());
  await page.fill("#nickname", "p8合帧");
  await page.click("[data-testid=auth-submit]");
  await page.waitForSelector("[data-testid=composer-input]");

  // [2] 发 200 段假流（delay 0：整burst到达，最能暴露合帧效果）
  await page.fill("[data-testid=composer-input]", `#fake {"chunks": ${CHUNKS}, "delay_ms": 0} 合帧性能冒烟`);
  await page.click("[data-testid=send-btn]");
  await page.waitForURL(/\/t\//, { timeout: 15000 });
  check("发送后跳任务页", /\/t\//.test(page.url()));

  // [3] 等流结束：usage 行出现在落库后的消息上
  await page.waitForSelector(".msg-ai .usage-line", { timeout: 30000 });
  check("流式回复完成", true);

  // [4] 读合帧统计
  const stats = await page.evaluate(() => window.__da_stream_stats || null);
  check("统计探针存在", Boolean(stats), JSON.stringify(stats));
  if (stats) {
    check(`收到事件数 >= ${CHUNKS}`, stats.received >= CHUNKS, `received=${stats.received}`);
    check("至少 flush 过一次", stats.flushes >= 1, `flushes=${stats.flushes}`);
    check(
      `压缩比 >= 5 倍（flushes ${stats.flushes} <= received/5 ${Math.floor(stats.received / 5)}）`,
      stats.flushes <= stats.received / 5,
      `received=${stats.received} flushes=${stats.flushes}`,
    );
  }

  // [5] 顺序 + 完整 + 尾帧：持久化正文必须严格等于 200 段按序拼接
  const taskId = page.url().split("/t/")[1];
  const detail = await page.request.get(BASE + `/api/tasks/${taskId}`).then((r) => r.json());
  const expected = Array.from({ length: CHUNKS }, (_, i) => `第${i + 1}段。`).join("");
  const assistant = (detail.messages || []).filter((m) => m.role === "assistant").pop();
  const content = assistant ? assistant.content : "";
  check("正文完整且按序（含尾帧）", content === expected,
    content === expected ? `${content.length} 字` : `期望 ${expected.length} 字，实得 ${content.length} 字`);

  await browser.close();
  console.log(`\n${FAIL.length ? "FAIL" : "PASS"}  ${PASS.length} ok / ${FAIL.length} failed`);
  process.exit(FAIL.length ? 1 : 0);
}

main().catch((err) => {
  console.error("p8 运行异常：", err);
  process.exit(1);
});
