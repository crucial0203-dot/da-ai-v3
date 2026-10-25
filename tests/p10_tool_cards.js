/** p10 工具调用卡：kind 图标/标签 + call_id 合并 + 同类折叠 + 思考块折叠。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p10_tool_cards.js
 *
 * 全部走真实链路：浏览器登录 → 建任务 → 对话框发 #fake #tool 控制串 →
 * 假上游驱动工具回路 → 断言 SSE 事件经 applyLiveEvent 合并后的渲染结果。
 *
 * 时序说明：假上游默认 40ms 一段，整条流 <2s 就结束、live 卡片被清空，
 * 所以控制串带 delay_ms=2500 拉宽窗口，断言都在流进行中做（不等流结束）。
 *
 * 断言：
 *   ① 单轮调用：一张卡，kind=search、label=联网检索、终态 done
 *   ② 连续两轮同工具：折叠成「检索 ×2」组，展开后两张卡 call_id 不同
 *   ③ 白名单拒绝：error 卡带拒绝理由，kind=other
 *   ④ 思考块：流中自动展开，有正文后自动收起
 *   ⑤ done 卡的参数区默认收起（不把回答挤下去）
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
  const adminCookie = (login.headers.get("set-cookie") || "").split(";")[0];
  const inv = await fetch(BASE + "/api/admin/invites", {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: adminCookie },
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 50, note: "p10" }),
  }).then((r) => r.json());
  const code = inv.codes[0];

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  page.setDefaultTimeout(30000);

  // [1] 进站
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.fill("#code", code.toLowerCase());
  await page.fill("#nickname", "p10工具卡");
  await page.click("[data-testid=auth-submit]");
  await page.waitForSelector("[data-testid=history-list]");

  /** 建任务、打开、发消息。断言在流中进行，不在此处等结束。 */
  async function sendToolTurn(text) {
    const r = await page.request.post(BASE + "/api/tasks", {
      data: { goal: "p10 工具卡", mode: "research", title: "p10" },
    });
    const task = await r.json();
    const id = task.id || (task.task || {}).id;
    await page.goto(BASE + "/t/" + id, { waitUntil: "networkidle" });
    await page.waitForSelector("[data-testid=composer-input]");
    await page.fill("[data-testid=composer-input]", text);
    await page.click("[data-testid=send-btn]");
    return id;
  }

  /** 等这一轮的流彻底结束（live 卡片消失、历史刷新出来）。 */
  async function waitStreamEnd() {
    await page.waitForFunction(
      () => !document.querySelector("[data-testid=live-msg]"),
      { timeout: 60000 },
    );
  }

  // [2] 单轮：一张卡 + kind/label/终态 + 参数区收起
  await sendToolTurn('#fake #tool {"tool_name":"web_search","tool_input":{"query":"行政协议"},"delay_ms":2500}');
  await page.waitForSelector("[data-testid=tool-card][data-kind=search]", { timeout: 20000 });
  check("② 单轮渲染出 search 卡", true);
  const card = page.locator("[data-testid=tool-card][data-kind=search]").first();
  check("② label 显示「联网检索」", (await card.locator(".tool-label").innerText()).includes("联网检索"));
  check("② kind 徽标显示「检索」", (await card.locator(".tool-kind-chip").innerText()).includes("检索"));
  check("② 工具名以等宽小字附注", (await card.locator(".tool-name").innerText()).includes("web_search"));
  await page.waitForSelector("[data-testid=tool-card][data-kind=search][data-status=done]", { timeout: 20000 });
  check("② 终态 done", true);
  check("② done 卡参数区默认收起",
    (await card.locator(".tool-detail").count()) === 0
    || !(await card.locator(".tool-detail").first().evaluate((el) => el.open)),
    "参数区应收起");
  await waitStreamEnd();

  // [3] 连续两轮同工具：折叠成组，展开后两张卡 call_id 不同、都 done
  await sendToolTurn('#fake #tool {"tool_name":"web_search","tool_rounds":2,"delay_ms":2500}');
  await page.waitForSelector("[data-testid=tool-group][data-kind=search]", { timeout: 25000 });
  check("③ 同类连续折叠成组", true);
  const group = page.locator("[data-testid=tool-group][data-kind=search]").first();
  const groupText = await group.locator("> summary").innerText();
  check("③ 组头显示「检索」与 ×2", groupText.includes("检索") && groupText.includes("2"),
    groupText.replace(/\n/g, " "));
  check("③ 组默认收起（不占屏幕）", !(await group.evaluate((el) => el.open)));
  await group.locator("> summary").click();
  await page.waitForTimeout(300);
  const inner = group.locator(".tool-group-body [data-testid=tool-card]");
  check("③ 展开后组内两张卡", (await inner.count()) === 2, `count=${await inner.count()}`);
  const ids = await inner.evaluateAll((els) => els.map((e) => e.getAttribute("data-call-id")));
  check("③ 两张卡 call_id 不同", ids.length === 2 && ids[0] && ids[1] && ids[0] !== ids[1],
    ids.join(" / "));
  await page.waitForFunction(
    () => {
      const cards = document.querySelectorAll("[data-testid=tool-group][data-kind=search] [data-testid=tool-card]");
      return cards.length === 2 && [...cards].every((c) => c.getAttribute("data-status") === "done");
    },
    { timeout: 30000 },
  );
  check("③ 组内两张卡都 done", true);
  await waitStreamEnd();

  // [4] 白名单拒绝：error 卡 + 理由 + kind=other + 参数区自动展开
  await sendToolTurn('#fake #tool {"tool_name":"shell.execute","tool_input":{"cmd":"ls"},"delay_ms":2500}');
  await page.waitForSelector("[data-testid=tool-card][data-status=error]", { timeout: 20000 });
  check("④ 越权调用渲染 error 卡", true);
  // 一次 evaluate 原子取全字段：拒绝走 break，流在 error 事件后立刻结束，
  // 约 400ms 后 live 视图被清空（历史消息不渲染工具卡）——分多次 round-trip
  // 会在高负载下超过这个窗口，后面的断言打到已销毁的 DOM 上（flaky）。
  const errInfo = await page.evaluate(() => {
    const card = document.querySelector("[data-testid=tool-card][data-status=error]");
    if (!card) return null;
    const detail = card.querySelector(".tool-detail");
    return {
      kind: card.getAttribute("data-kind"),
      statusText: (card.querySelector(".tool-status-text") || {}).textContent || "",
      detailCount: card.querySelectorAll(".tool-detail").length,
      detailOpen: detail ? detail.open : null,
    };
  });
  check("④ error 卡 kind=other",
    !!errInfo && errInfo.kind === "other",
    errInfo ? errInfo.kind : "卡片已不在");
  check("④ 错误理由是白名单",
    !!errInfo && errInfo.statusText.includes("白名单"),
    errInfo ? errInfo.statusText : "卡片已不在");
  check("④ error 卡参数区自动展开（复现要看参数）",
    !!errInfo && errInfo.detailCount > 0 && errInfo.detailOpen === true,
    errInfo ? `count=${errInfo.detailCount} open=${errInfo.detailOpen}` : "卡片已不在");
  await waitStreamEnd();

  // [5] 思考块：流中自动展开，有正文后自动收起；手动展开不被自动规则抢走。
  // 用不带工具的纯流：工具轮里 text 事件携带的是工具调用块，answer 会立刻
  // 非空（那是工具回路的行为，不是思考块的），验证思考块要用纯文本流。
  await sendToolTurn('#fake {"delay_ms":2500,"chunks":4}');
  await page.waitForSelector("[data-testid=thinking-block]", { timeout: 20000 });
  check("⑤ 思考块渲染", true);
  const openWhileStreaming = await page.locator("[data-testid=thinking-block]").evaluate((el) => el.open);
  check("⑤ 无正文时思考块自动展开", openWhileStreaming === true, `open=${openWhileStreaming}`);
  await page.waitForFunction(
    () => {
      const el = document.querySelector("[data-testid=thinking-block]");
      const body = document.querySelector("[data-testid=live-msg] .msg-body");
      return el && body && body.textContent.length > 0 && !el.open;
    },
    { timeout: 30000 },
  );
  check("⑤ 有正文后思考块自动收起", true);
  // 手动展开：有正文在场，也不该被自动规则抢走（override 生效）
  await page.locator("[data-testid=thinking-block] > summary").click();
  await page.waitForTimeout(1500);
  const stillOpen = await page.locator("[data-testid=thinking-block]").evaluate((el) => el.open);
  check("⑤ 手动展开后不被自动收起抢走", stillOpen === true, `open=${stillOpen}`);

  // [6] 审批卡三段式：标题=动作 / 警告=风险 / 影响面逐字段；
  //     按钮 Deny(幽灵) → Allow once(实心)，共 2 个，没有 Always allow
  await sendToolTurn('#fake #tool {"tool_name":"doc_generate","tool_input":{"doc_type":"docx","goal":"一份行政协议纪要"},"delay_ms":2500}');
  await page.waitForSelector("[data-testid=tool-approval-card]", { timeout: 25000 });
  check("⑥ 审批卡出现", true);
  const ap = page.locator("[data-testid=tool-approval-card]");
  check("⑥ state=request", (await ap.getAttribute("data-state")) === "request",
    await ap.getAttribute("data-state"));
  check("⑥ 写操作 risk=high（destructive 红底）",
    (await ap.getAttribute("data-risk")) === "high", await ap.getAttribute("data-risk"));
  const apTitle = await ap.locator(".approval-title").innerText();
  check("⑥ 标题=动作（含工具名，p5 依赖）", apTitle.includes("doc_generate") && apTitle.includes("生成文档"),
    apTitle.replace(/\n/g, " "));
  check("⑥ 警告=风险（reason 警示带）",
    (await ap.locator("[data-testid=approval-reason]").count()) === 1);
  const detailRows = ap.locator("[data-testid=approval-details] .approval-detail-row");
  check("⑥ 影响面逐字段（doc_type/goal 两行）", (await detailRows.count()) === 2,
    `rows=${await detailRows.count()}`);
  check("⑥ 影响面值来自 input",
    (await ap.locator("[data-testid=approval-details]").innerText()).includes("docx"));
  const deny = ap.locator("[data-testid=tool-reject-btn]");
  const allow = ap.locator("[data-testid=tool-approve-btn]");
  check("⑥ 恰好 2 个按钮（没有 Always allow）", (await ap.locator("button").count()) === 2,
    `buttons=${await ap.locator("button").count()}`);
  check("⑥ Deny 是幽灵按钮", await deny.evaluate((el) => el.classList.contains("btn-ghost")));
  check("⑥ Allow once 是实心主按钮", await allow.evaluate((el) => el.classList.contains("btn-primary")));
  check("⑥ Allow 文案写清动作（允许执行这一次）",
    (await allow.innerText()).includes("允许执行这一次"), await allow.innerText());
  // 拒绝：卡片消失（p5 的同链路断言，这里顺带覆盖）
  await deny.click();
  await page.waitForFunction(
    () => !document.querySelector("[data-testid=tool-approval-card]"),
    { timeout: 15000 },
  );
  check("⑥ 拒绝后审批卡消失", true);

  await browser.close();
  console.log(`\n${FAIL.length ? "FAIL" : "PASS"}  ${PASS.length} ok / ${FAIL.length} failed`);
  process.exit(FAIL.length ? 1 : 0);
}

main().catch((err) => {
  console.error("p10 运行异常：", err);
  process.exit(1);
});
