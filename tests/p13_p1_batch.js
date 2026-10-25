/** p13 P1-7/P1-8/P1-9 批次验收：制品卡、配额横幅三态、上下文压力条、记忆 chips。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p13_p1_batch.js
 *
 * 覆盖：
 *   [1] P1-7 制品卡 generating（fence_ms 拉宽工具围栏 → MutationObserver 原子记录
 *       「正在写」+ 实时字数）与 done 卡（标题/预览 iframe/沙箱，保 p5 锚点）
 *   [2] P1-8 上下文压力条：历史段按**对话轮次**计（工具回注不算独立一轮）：
 *       首轮 ×0 → 第二轮 ×1 → 第三轮 ×2，且流结束后压力条不消失
 *   [3] P1-8 配额横幅三态：exhausted（1/1，附 429 错误卡）/ tight（9/10）/ normal（3/30）
 *   [4] P1-9 记忆 chips（新增 ×1）+ 记忆页三态徽标（新增/更新/已存在）+ 忘记按钮
 *
 * 时限说明：吃紧态要 used/quota ≥ 90%（9/10），而聊天限流是 6 次/60s，
 * 所以预消耗与浏览器验收并行跑，中间等一次限流窗口（61s）。
 */
const { chromium } = require("playwright-core");
const fs = require("fs"), path = require("path");
const ROOT = "D:/workbuddyGZ/da-ai-v3";
const BASE = "http://127.0.0.1:8020";
const SHOTS = path.join(ROOT, "tests/artifacts");

function env(k) {
  return (fs.readFileSync(path.join(ROOT, ".env"), "utf8").split(/\r?\n/)
    .find((l) => l.startsWith(k + "=")) || "").slice(k.length + 1).trim();
}

const PASS = [], FAIL = [];
function check(name, ok, detail = "") {
  (ok ? PASS : FAIL).push(name);
  console.log(`${ok ? "  OK " : "  !! "} ${name}${detail ? `  [${detail}]` : ""}`);
}
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// ---------- HTTP 侧（Node fetch） ----------

async function adminLogin() {
  const res = await fetch(BASE + "/api/admin/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: env("ADMIN_PASSWORD") }),
  });
  if (!res.ok) throw new Error("管理员登录失败");
  return (res.headers.get("set-cookie") || "").split(";")[0];
}

async function makeInvite(adminCookie, quota, note) {
  const res = await fetch(BASE + "/api/admin/invites", {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: adminCookie },
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: quota, note }),
  });
  const d = await res.json();
  if (!res.ok) throw new Error(`建邀请码失败：${JSON.stringify(d)}`);
  return d.codes[0];
}

/** 进站并返回 da_sid（每个账号一个独立 cookie，互不干扰）。 */
async function httpJoin(code, nickname) {
  const res = await fetch(BASE + "/api/join", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: code.toLowerCase(), nickname }),
  });
  const setCookie = res.headers.get("set-cookie") || "";
  const sid = (setCookie.split(";")[0] || "").split("=")[1] || "";
  if (!res.ok || !sid) throw new Error(`HTTP 进站失败：${res.status} ${await res.text()}`);
  return sid;
}

async function httpCreateTask(sid, goal) {
  const res = await fetch(BASE + "/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: `da_sid=${sid}` },
    body: JSON.stringify({ mode: "research", goal }),
  });
  const d = await res.json();
  if (!res.ok) throw new Error(`建任务失败：${JSON.stringify(d)}`);
  // 建任务返回 {ok, task:{...}}，id 在 task 里（m25 task_id_of 同款兼容）
  const taskId = d.id || (d.task && d.task.id) || "";
  if (!taskId) throw new Error(`建任务响应里没有 id：${JSON.stringify(d).slice(0, 200)}`);
  return taskId;
}

/** 发一轮对话并读完整个 SSE（fake 流很短，一次读完即可）。 */
async function httpChat(sid, taskId, text) {
  const res = await fetch(`${BASE}/api/tasks/${taskId}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: `da_sid=${sid}` },
    body: JSON.stringify({ text, web_search: false }),
  });
  const body = await res.text();
  return { status: res.status, body };
}

/** 预消耗配额：聊天限流 6 次/60s（deps.limit_chat），第 7 发前等窗口滑过。
 *
 *  返回 sid（会话 cookie 值）——调用方要把**同一个**用户带进浏览器，
 *  重新 join 会拿到另一个访客，配额计数对不上（吃紧态就测不出来）。 */
async function burnQuota(code, total, nickname) {
  const sid = await httpJoin(code, nickname);
  const taskId = await httpCreateTask(sid, "p13 配额预消耗");
  for (let i = 0; i < total; i++) {
    if (i === 6) {
      console.log(`  .. 预消耗 ${i}/${total}，等限流窗口滑过（61s）`);
      await sleep(61000);
    }
    const r = await httpChat(sid, taskId, `#fake 预消耗第 ${i + 1} 发`);
    if (r.status !== 200) throw new Error(`预消耗第 ${i + 1} 发失败：${r.status} ${r.body.slice(0, 200)}`);
  }
  console.log(`  .. 预消耗完成 ${total}/${total}`);
  return sid;
}

// ---------- 浏览器侧 ----------

/** 起一个带「瞬时 DOM 记录器」的页面：generating 卡/chips/错误卡都只活几百毫秒，
 *  断言必须原子捕获（p10 ④ 的教训），MutationObserver 在 document-start 就装好。 */
async function launch() {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  await page.addInitScript(() => {
    window.__p13 = {
      art: { seen: 0, states: [], words: [], texts: [] },
      chips: { seen: 0, texts: [] },
      errors: { seen: 0, texts: [] },
    };
    const push = (arr, txt) => { if (arr.length < 30) arr.push(txt); };
    const record = () => {
      document.querySelectorAll("[data-testid=artifact-card]").forEach((el) => {
        const st = el.getAttribute("data-state") || "";
        const txt = (el.innerText || "").replace(/\s+/g, " ");
        const m = txt.match(/(\d+)\s*字/);
        window.__p13.art.seen++;
        if (st && !window.__p13.art.states.includes(st)) window.__p13.art.states.push(st);
        window.__p13.art.words.push(m ? parseInt(m[1], 10) : -1);
        push(window.__p13.art.texts, txt);
      });
      document.querySelectorAll("[data-testid=memory-chips]").forEach((el) => {
        window.__p13.chips.seen++;
        push(window.__p13.chips.texts, (el.innerText || "").replace(/\s+/g, " "));
      });
      document.querySelectorAll("[data-testid=error-state]").forEach((el) => {
        window.__p13.errors.seen++;
        push(window.__p13.errors.texts, (el.innerText || "").replace(/\s+/g, " "));
      });
    };
    // ⚠️ document-start 时 document.documentElement 还不存在（readyState=loading），
    //  observe(documentElement) 会抛 "parameter 1 is not of type 'Node'" 且观察器永不安装
    //  → 记录恒为 0。改观察 document 本身（document 永远是 Node），subtree 覆盖全树。
    new MutationObserver(record).observe(document, {
      childList: true, subtree: true, characterData: true,
    });
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", record);
    else record();
  });
  return { browser, context, page };
}

async function joinUI(page, code, nickname) {
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.fill("#code", code.toLowerCase());
  await page.fill("#nickname", nickname);
  await page.click("[data-testid=auth-submit]");
  await page.waitForSelector("[data-testid=composer-input]");
  // 关联网：fake 链路用不到，省掉每条流的真实检索
  const web = page.locator("[data-testid=web-toggle]");
  if (await web.count()) {
    const cls = (await web.getAttribute("class")) || "";
    if (/\bon\b/.test(cls)) await web.click();
  }
}

/** 用已有的会话 sid 进浏览器（不重新 join）。
 *
 *  配额是按**访客**算的：预消耗和验收必须用同一个访客，
 *  重新 join 会拿到另一个新访客，used 归零，吃紧态就测不出来。 */
async function enterWithSid(context, sid) {
  await context.addCookies([{
    name: "da_sid", value: sid, domain: "127.0.0.1", path: "/",
  }]);
  const page = context.pages()[0] || (await context.newPage());
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.waitForSelector("[data-testid=composer-input]");
  return page;
}

async function sendFromHome(page, text) {
  await page.fill("[data-testid=composer-input]", text);
  await page.click("[data-testid=send-btn]");
  await page.waitForURL(/\/t\//, { timeout: 20000 });
}

async function sendOnTask(page, text) {
  await page.fill("[data-testid=composer-input]", text);
  await page.click("[data-testid=send-btn]");
}

/** 等流结束：composer 从「停止」换回「发送」。 */
async function waitStreamEnd(page) {
  await page.waitForSelector("[data-testid=send-btn]", { state: "visible", timeout: 40000 });
}

async function readRec(page) {
  return page.evaluate(() => window.__p13);
}

/** 取记忆页某一行的徽标文字（无行/无徽标分别给可诊断的占位）。 */
async function badgeOf(page, text) {
  return page.evaluate((t) => {
    const items = [...document.querySelectorAll("[data-testid=memory-item]")];
    const item = items.find((el) => (el.textContent || "").includes(t));
    if (!item) return "(无此行)";
    const badge = item.querySelector("[data-testid=memory-change]");
    return badge ? (badge.textContent || "").trim() : "(无徽标)";
  }, text);
}

async function memoryApiList(sid) {
  const res = await fetch(`${BASE}/api/memory`, { headers: { Cookie: `da_sid=${sid}` } });
  return (await res.json()).memories || [];
}

async function main() {
  const adminCookie = await adminLogin();
  const codeMain = await makeInvite(adminCookie, 30, "p13主");
  const codeQ1 = await makeInvite(adminCookie, 1, "p13耗尽");
  const codeQ9 = await makeInvite(adminCookie, 10, "p13吃紧");
  const codeMem = await makeInvite(adminCookie, 30, "p13记忆");
  console.log(`邀请码：主=${codeMain} 耗尽=${codeQ1} 吃紧=${codeQ9} 记忆=${codeMem}`);

  // 吃紧账号预消耗 9 发（内含 61s 限流等待）——与下面的浏览器验收并行
  const tightBurn = burnQuota(codeQ9, 9, "p13吃紧").catch((err) => {
    console.error("预消耗失败：" + err.message);
    throw err;
  });

  // ===== A：P1-7 制品卡 + P1-8 上下文压力条 =====
  console.log("\n-- A：制品卡与压力条 --");
  const A = await launch();
  await joinUI(A.page, codeMain, "p13主");

  // [1] 制品卡：fence_ms=300 把工具围栏分 4 段吐（默认 0 与从前逐字节一致），
  // 工具卡在围栏闭合后才出现，generating 窗口≈工具执行时长，用 MutationObserver 原子记录。
  const ctrl = {
    tool_name: "artifact_create",
    tool_input: {
      title: "p13制品卡",
      html: "<html><body><h1>p13 artifact</h1><p>预览内容</p></body></html>",
    },
    fence_ms: 300,
  };
  await sendFromHome(A.page, `#fake #tool ${JSON.stringify(ctrl)} 生成一个制品`);
  await waitStreamEnd(A.page);
  const rec = await readRec(A.page);
  const maxWords = rec.art.words.length ? Math.max(...rec.art.words) : -1;
  check("[1] generating 卡出现过", rec.art.seen >= 1, `seen=${rec.art.seen}`);
  check("[1] generating 态记录在案", rec.art.states.includes("generating"), rec.art.states.join(","));
  check("[1] 实时字数非零（围栏约 160 字）", maxWords >= 100, `max=${maxWords}`);
  check("[1] 「正在写」微光标签", rec.art.texts.some((t) => t.includes("正在写")),
    (rec.art.texts[0] || "").slice(0, 50));

  // done 卡走侧栏 artifact-row（p5 锚点原样保留）
  await A.page.waitForSelector("[data-testid=artifact-row]", { timeout: 15000 });
  const artRows = await A.page.locator("[data-testid=artifact-row]").count();
  check("[1] 侧栏 done 卡出现", artRows >= 1, `${artRows} 件`);
  const artTitle = await A.page.locator("[data-testid=artifact-row]").first().innerText();
  check("[1] done 卡带标题", artTitle.includes("p13制品卡"), artTitle.replace(/\s+/g, " ").slice(0, 40));
  await A.page.locator("[data-testid=artifact-toggle]").first().click();
  const frame = A.page.locator(".artifact-frame");
  check("[1] iframe 预览挂载", (await frame.count()) === 1);
  check("[1] iframe 无 same-origin（安全）", (await frame.getAttribute("sandbox")) === "");
  await A.page.screenshot({ path: path.join(SHOTS, "p13_artifact.png"), fullPage: true });

  // [2] 上下文压力条的历史段口径是**对话轮次**（不是消息条数）：
  //     工具结果回注不算独立一轮。制品轮是首轮 → 无历史段；
  //     再发两轮 → ×1、×2。流结束后压力条靠 lastCtx 留在输入区上方。
  //
  //     计数为 0 的段不渲染（压力条只显示真正占位的部分），
  //     所以首轮是「没有历史段」而不是「历史 ×0」。
  await A.page.waitForSelector("[data-testid=context-pressure]", { timeout: 10000 });
  const ctx0 = (await A.page.locator("[data-testid=context-pressure]").innerText()).replace(/\s+/g, " ");
  check("[2] 首轮无历史段", !/历史/.test(ctx0), ctx0.slice(0, 80));
  check("[2] 首轮仍有其他段（技能）", /技能/.test(ctx0), ctx0.slice(0, 80));

  await A.page.waitForTimeout(700); // 等 onEnd 的 reload 落定再发下一轮
  await sendOnTask(A.page, "#fake 第一轮普通对话");
  await waitStreamEnd(A.page);
  await A.page.waitForSelector("[data-testid=context-pressure]");
  const ctx1 = (await A.page.locator("[data-testid=context-pressure]").innerText()).replace(/\s+/g, " ");
  check("[2] 历史段 ×1", /历史\s*×1/.test(ctx1), ctx1.slice(0, 80));
  check("[2] 非吃紧态", (await A.page.locator("[data-testid=context-pressure]").getAttribute("data-tight")) === "0");

  await A.page.waitForTimeout(700);
  await sendOnTask(A.page, "#fake 第二轮普通对话");
  await waitStreamEnd(A.page);
  await A.page.waitForTimeout(700); // 流结束后压力条应靠 lastCtx 留在输入区上方
  const ctx2 = (await A.page.locator("[data-testid=context-pressure]").innerText()).replace(/\s+/g, " ");
  check("[2] 历史段随轮次增长 ×2", /历史\s*×2/.test(ctx2), ctx2.slice(0, 80));
  check("[2] 流结束后压力条不消失", (await A.page.locator("[data-testid=context-pressure]").count()) === 1);
  await A.page.screenshot({ path: path.join(SHOTS, "p13_context.png"), fullPage: true });

  // ===== D：P1-9 记忆 chips 与三态徽标 =====
  console.log("\n-- D：记忆 --");
  const D = await launch();
  await joinUI(D.page, codeMem, "p13记忆");
  const memSid = (await D.context.cookies()).find((c) => c.name === "da_sid").value;

  await sendFromHome(D.page, "#fake #mem 记住我喜欢简洁直接的回答");
  await waitStreamEnd(D.page);
  const drec = await readRec(D.page);
  check("[4] 记忆 chips 出现过", drec.chips.seen >= 1, `seen=${drec.chips.seen}`);
  check("[4] chips 显示「新增 ×1」", drec.chips.texts.some((t) => t.includes("新增") && t.includes("×1")),
    (drec.chips.texts[0] || "").slice(0, 40));

  // 回合后的异步评审也会记同一句 → 去重命中 → change 翻成 existing（API 级轮询，确定性）
  let flipped = false;
  for (let i = 0; i < 25; i++) {
    const rows = await memoryApiList(memSid);
    const row = rows.find((m) => (m.content || "").includes("简洁直接"));
    if (row && row.change === "existing") { flipped = true; break; }
    await sleep(300);
  }
  check("[4] 异步评审判重（existing）", flipped);

  await D.page.click('a:text-is("记忆")');
  await D.page.waitForSelector("[data-testid=memory-item]");
  check("[4] 记忆页渲染「已存在」徽标", (await badgeOf(D.page, "简洁直接")) === "已存在",
    await badgeOf(D.page, "简洁直接"));

  // 新增徽标：HTTP 加一条（评审不会碰它），刷新后徽标应保持「新增」
  const addRes = await fetch(`${BASE}/api/memory`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Cookie: `da_sid=${memSid}` },
    body: JSON.stringify({ content: "p13徽标新增测试" }),
  });
  check("[4] HTTP 添加记忆 200", addRes.status === 200, String(addRes.status));
  await D.page.reload();
  await D.page.waitForSelector("[data-testid=memory-item]");
  check("[4] 新增徽标", (await badgeOf(D.page, "p13徽标新增测试")) === "新增",
    await badgeOf(D.page, "p13徽标新增测试"));

  // 更新徽标：PATCH 后刷新 → 「更新」
  const rows2 = await memoryApiList(memSid);
  const row2 = rows2.find((m) => m.content === "p13徽标新增测试");
  const patchRes = await fetch(`${BASE}/api/memory/${row2.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", Cookie: `da_sid=${memSid}` },
    body: JSON.stringify({ content: "p13徽标更新测试" }),
  });
  check("[4] HTTP 更新记忆 200", patchRes.status === 200, String(patchRes.status));
  await D.page.reload();
  await D.page.waitForSelector("[data-testid=memory-item]");
  check("[4] 更新徽标", (await badgeOf(D.page, "p13徽标更新测试")) === "更新",
    await badgeOf(D.page, "p13徽标更新测试"));

  // 忘记按钮：确认弹窗 → 行消失
  //
  // 断言口径跟 p6 一致：等 paint 落定（800ms）再数行数。
  // 不能用 waitForFunction(count < before)——删除后 paint() 会 setLoading(true)，
  // 列表整个消失、count 变 0，条件立刻满足，测到的是 loading 态而不是删除结果。
  const before = await D.page.locator("[data-testid=memory-item]").count();
  await D.page.locator("[data-testid=memory-item]", { hasText: "p13徽标更新测试" })
    .locator("[data-testid=memory-forget]").click();
  await D.page.locator(".modal-actions .btn-primary").click();
  await D.page.waitForTimeout(1200);
  const after = await D.page.locator("[data-testid=memory-item]").count();
  check("[4] 忘记后行数减少", after === before - 1, `${before} → ${after}`);
  check("[4] 被忘记的那条已消失",
    (await D.page.locator("[data-testid=memory-item]", { hasText: "p13徽标更新测试" }).count()) === 0);
  await D.page.screenshot({ path: path.join(SHOTS, "p13_memory.png"), fullPage: true });
  await D.browser.close();

  // ===== B：配额耗尽 =====
  console.log("\n-- B：配额耗尽 --");
  const B = await launch();
  await joinUI(B.page, codeQ1, "p13耗尽");
  await sendFromHome(B.page, "#fake 唯一的一发");
  await waitStreamEnd(B.page);
  await B.page.reload(); // 横幅只在挂载时取数，刷新后才是 1/1
  await B.page.waitForSelector("[data-testid=usage-bar]");
  const barB = B.page.locator("[data-testid=usage-bar]");
  check("[3] 耗尽态 data-state", (await barB.getAttribute("data-state")) === "exhausted");
  const txtB = (await barB.innerText()).replace(/\s+/g, " ");
  check("[3] 耗尽文案（1/1 + 已用完 + 重置）",
    txtB.includes("今日 1 / 1 次") && txtB.includes("已用完") && /(小时|分钟)/.test(txtB), txtB);

  // 耗尽后再发一发 → 429 结构化错误（错误卡只活约 400ms，靠记录器捕获）
  await sendOnTask(B.page, "#fake 第二发");
  await sleep(1500);
  const brec = await readRec(B.page);
  check("[3] 429 错误卡出现过", brec.errors.seen >= 1, `seen=${brec.errors.seen}`);
  check("[3] 429 文案带「额度」", brec.errors.texts.some((t) => t.includes("额度")),
    (brec.errors.texts[0] || "").slice(0, 60));
  await B.page.screenshot({ path: path.join(SHOTS, "p13_quota_exhausted.png"), fullPage: true });
  await B.browser.close();

  // ===== C：配额吃紧（等预消耗完成） =====
  console.log("\n-- C：配额吃紧 --");
  const tightSid = await tightBurn;
  const C = await launch();
  // 用预消耗的同一个会话进浏览器——配额按访客算，重新 join 是另一个访客
  const CPage = await enterWithSid(C.context, tightSid);
  await CPage.waitForSelector("[data-testid=usage-bar]");
  const barC = CPage.locator("[data-testid=usage-bar]");
  check("[3] 吃紧态 data-state", (await barC.getAttribute("data-state")) === "tight",
    await barC.getAttribute("data-state"));
  const txtC = (await barC.innerText()).replace(/\s+/g, " ");
  check("[3] 吃紧文案（9/10 + 即将用尽）",
    txtC.includes("今日 9 / 10 次") && txtC.includes("即将用尽"), txtC);
  await CPage.screenshot({ path: path.join(SHOTS, "p13_quota_tight.png"), fullPage: true });
  await C.browser.close();

  // ===== A 收尾：普通态 =====
  console.log("\n-- A 收尾：普通态 --");
  await A.page.reload();
  await A.page.waitForSelector("[data-testid=usage-bar]");
  const barA = A.page.locator("[data-testid=usage-bar]");
  check("[3] 普通态 data-state", (await barA.getAttribute("data-state")) === "normal");
  const txtA = (await barA.innerText()).replace(/\s+/g, " ");
  check("[3] 普通文案（3/30）", txtA.includes("今日 3 / 30 次"), txtA);
  await A.page.screenshot({ path: path.join(SHOTS, "p13_quota_normal.png"), fullPage: true });
  await A.browser.close();

  console.log(`\n== p13: ${PASS.length} 过 / ${FAIL.length} 败 ==`);
  if (FAIL.length) { FAIL.forEach((f) => console.log("  - " + f)); process.exit(1); }
}

main().catch((err) => { console.error(err); process.exit(2); });
