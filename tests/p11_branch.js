/** p11 停止卡与分支切换器：真实浏览器点按钮。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p11_branch.js
 *
 * 覆盖（P1-4 / P1-5 的前端面）：
 *   A 流式中点停止 → 停止卡（已停止 · 保留了 N 字）→ 继续写 → 正文接上且更长
 *   B 流式中点停止 → 丢弃 → 卡片消失、会话里不留这条 AI 消息
 *   C 连点两次重新生成 → 分支切换器 ← 3/3 →：三个版本内容互不相同、只显示一条
 *
 * 联网开关先关掉：fake 上游不依赖检索，关了每条流少一次真实搜索，测试更快也更稳。
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

async function login(page, code, nickname) {
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.fill("#code", code.toLowerCase());
  await page.fill("#nickname", nickname);
  await page.click("[data-testid=auth-submit]");
  await page.waitForSelector("[data-testid=composer-input]");
  // 关联网（默认开着）：fake 链路用不到，还能省掉每条流的真实检索
  const web = page.locator("[data-testid=web-toggle]");
  if (await web.count()) {
    const cls = (await web.getAttribute("class")) || "";
    if (/\bon\b/.test(cls)) await web.click();
  }
}

async function sendFromHome(page, text) {
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.waitForSelector("[data-testid=composer-input]");
  await page.fill("[data-testid=composer-input]", text);
  await page.click("[data-testid=send-btn]");
  await page.waitForURL(/\/t\//, { timeout: 20000 });
}

/** 等流结束：composer 从「停止」换回「发送」。 */
async function waitStreamEnd(page) {
  await page.waitForSelector("[data-testid=send-btn]", { state: "visible", timeout: 40000 });
}

/** 点重新生成并等界面真正换代。
 *  send-btn 回来只说明流结束，刷新要再等 400ms——不等换代就断言，
 *  读到的是上一版的 DOM（分支版数会少算，p11 C 段的教训）。 */
async function regenerateAndSettle(page) {
  const before = await page.locator("[data-testid=msg-ai]").first().getAttribute("data-msg-id");
  await page.click("[data-testid=regen-btn]");
  await waitStreamEnd(page);
  await page.waitForFunction(
    (prev) => {
      const el = document.querySelector("[data-testid=msg-ai]");
      return !!el && el.getAttribute("data-msg-id") !== prev;
    },
    before,
    { timeout: 30000 },
  );
}

async function main() {
  const adminRes = await fetch(BASE + "/api/admin/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: env("ADMIN_PASSWORD") }),
  });
  const adminCookie = (adminRes.headers.get("set-cookie") || "").split(";")[0];
  const inv = await fetch(BASE + "/api/admin/invites", {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: adminCookie },
    body: JSON.stringify({ count: 2, max_uses: 5, days: 7, daily_quota: 20, note: "p11" }),
  }).then((r) => r.json());
  const [code1, code2] = inv.codes;

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(20000);

  const aiText = () => page.locator("[data-testid=msg-ai] .md").first().textContent();

  // ================= A：停止 → 继续写 =================
  await login(page, code1, "p11停止");
  // 6 段 × 400ms：够在流式中途点停止
  await sendFromHome(page, '#fake {"chunks": 6, "delay_ms": 400} 停止后续写');
  await page.waitForSelector("[data-testid=live-msg]");
  // 等到直播里出现第一段再点停止（固定时长的等待会把 Race 测成 Bug，p5 的教训）
  await page.waitForFunction(
    () => (document.querySelector("[data-testid=live-msg]")?.textContent || "").includes("第1段"),
    { timeout: 30000 },
  );
  await page.click("[data-testid=stop-btn]");
  await page.waitForSelector("[data-testid=stopped-run]", { timeout: 30000 });
  check("A 停止卡出现", true);
  const note = await page.locator("[data-testid=stopped-run] .stopped-run-note").textContent();
  check("A 停止卡显示保留字数", /已停止 · 保留了 \d+ 字/.test(note || ""), note || "");
  const half = (await aiText()) || "";
  check("A 半截正文上屏（有第1段、无第6段）", half.includes("第1段") && !half.includes("第6段"), half.slice(0, 60));

  await page.click("[data-testid=continue-btn]");
  await waitStreamEnd(page);
  // 续跑完成 → 刷新 → 新消息取代旧半截
  await page.waitForFunction(
    () => !document.querySelector("[data-testid=stopped-run]"),
    { timeout: 30000 },
  );
  check("A 继续写后停止卡消失", true);
  const full = (await aiText()) || "";
  check("A 续写接上了半截（第1段出现两次）",
    (full.match(/第1段/g) || []).length >= 2, full.slice(0, 80));
  check("A 续写写完全部 6 段", full.includes("第6段"), full.slice(-60));
  // 续跑结果与旧半截同组：切换器应显示 2 版
  const posA = await page.locator("[data-testid=branch-pos]").textContent();
  check("A 续跑占旧槽位（第 2/2 版）", (posA || "").replace(/\s/g, "") === "第2/2版", posA || "");

  // ================= B：停止 → 丢弃 =================
  await sendFromHome(page, '#fake {"chunks": 6, "delay_ms": 400} 停止后丢弃');
  await page.waitForSelector("[data-testid=live-msg]");
  await page.waitForFunction(
    () => (document.querySelector("[data-testid=live-msg]")?.textContent || "").includes("第1段"),
    { timeout: 30000 },
  );
  await page.click("[data-testid=stop-btn]");
  await page.waitForSelector("[data-testid=stopped-run]", { timeout: 30000 });
  await page.click("[data-testid=discard-btn]");
  await page.waitForFunction(
    () => !document.querySelector("[data-testid=stopped-run]"),
    { timeout: 30000 },
  );
  check("B 丢弃后停止卡消失", true);
  const aiCount = await page.locator("[data-testid=msg-ai]").count();
  check("B 丢弃后会话里不留这条 AI 消息", aiCount === 0, `msg-ai=${aiCount}`);

  // ================= C：连点重生成 → 分支切换 =================
  await page.request.post(BASE + "/api/auth/logout").catch(() => undefined);
  await login(page, code2, "p11分支");
  await sendFromHome(page, '#fake #vary {"chunks": 3} 分支切换');
  await waitStreamEnd(page);
  await page.waitForSelector("[data-testid=msg-ai]", { timeout: 30000 });
  const v1 = (await aiText()) || "";

  await regenerateAndSettle(page);
  await regenerateAndSettle(page);

  await page.waitForSelector("[data-testid=branch-switch]", { timeout: 30000 });
  check("C 分支切换器出现", true);
  const pos1 = (await page.locator("[data-testid=branch-pos]").textContent() || "").replace(/\s/g, "");
  check("C 默认显示最新版（第 3/3 版）", pos1 === "第3/3版", pos1);
  const aiCountC = await page.locator("[data-testid=msg-ai]").count();
  check("C 三个版本只显示一条", aiCountC === 1, `msg-ai=${aiCountC}`);
  const v3 = (await aiText()) || "";
  check("C 最新版与首版不同", v3 !== v1, `${v1.slice(0, 24)} vs ${v3.slice(0, 24)}`);

  await page.click("[data-testid=branch-prev]");
  await page.waitForFunction(
    () => (document.querySelector("[data-testid=branch-pos]")?.textContent || "").replace(/\s/g, "") === "第2/3版",
    { timeout: 10000 },
  );
  const v2 = (await aiText()) || "";
  check("C 切到第 2 版", true);
  check("C 第 2 版与首版/最新版都不同", v2 !== v1 && v2 !== v3,
    `${v1.slice(0, 20)} / ${v2.slice(0, 20)} / ${v3.slice(0, 20)}`);

  await page.click("[data-testid=branch-prev]");
  await page.waitForFunction(
    () => (document.querySelector("[data-testid=branch-pos]")?.textContent || "").replace(/\s/g, "") === "第1/3版",
    { timeout: 10000 },
  );
  const back1 = (await aiText()) || "";
  check("C 切回第 1 版且内容与首版一致", back1 === v1, back1.slice(0, 40));
  check("C 第 1 版时「上一版」禁用",
    await page.locator("[data-testid=branch-prev]").isDisabled());

  await page.click("[data-testid=branch-next]");
  await page.waitForFunction(
    () => (document.querySelector("[data-testid=branch-pos]")?.textContent || "").replace(/\s/g, "") === "第2/3版",
    { timeout: 10000 },
  );
  const fwd2 = (await aiText()) || "";
  check("C 前进回到第 2 版且内容一致", fwd2 === v2, fwd2.slice(0, 40));

  // 切到旧版后再重生成：pick 过期，应自动跳回最新版（第 4/4 版）
  await regenerateAndSettle(page);
  await page.waitForFunction(
    () => (document.querySelector("[data-testid=branch-pos]")?.textContent || "").replace(/\s/g, "") === "第4/4版",
    { timeout: 30000 },
  );
  check("C 旧版视图下重生成 → 自动跳最新版（第 4/4 版）", true);
  const v4 = (await aiText()) || "";
  check("C 第 4 版内容又是新的", v4 !== v1 && v4 !== v2 && v4 !== v3, v4.slice(0, 30));

  await browser.close();
  console.log("\n" + "=".repeat(46));
  console.log(`通过 ${PASS.length}/${PASS.length + FAIL.length}`);
  if (FAIL.length) {
    console.log("失败项：", FAIL);
    process.exit(1);
  }
}

main().catch((err) => {
  console.error("p11 运行异常：", err);
  process.exit(1);
});
