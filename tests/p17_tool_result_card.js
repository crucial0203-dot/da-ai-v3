/** p17 工具回注卡验收（修复「<<<TOOL_RESULT>>> 裸奔」）。
 *
 * 背景：后端把工具结果包成 <<<TOOL_RESULT name=...>>>{json}<<<END_TOOL_RESULT>>>
 * 以 user 消息回注给模型——线上格式，不是用户言论。修复前前端原样渲染，
 * 用户直接在对话里看到原始 JSON 与内部标记（库里已存 340 条）。
 *
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p17_tool_result_card.js
 * 前置：8020 已起（新构建的 bundle）。
 */
const { chromium } = require("playwright-core");
const fs = require("fs"), path = require("path");
const esbuild = require(path.join("D:/workbuddyGZ/da-ai-v3/web/node_modules/esbuild"));
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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function loadTs(rel) {
  const out = esbuild.buildSync({
    entryPoints: [path.join(ROOT, "web/src", rel)],
    bundle: true, format: "cjs", write: false,
  });
  const mod = { exports: {} };
  new Function("module", "exports", out.outputFiles[0].text)(mod, mod.exports);
  return mod.exports;
}

/** Part A：解析纯函数 */
async function testParser() {
  console.log("\n-- A：回注块解析纯函数 --");
  const { splitToolResults, summarizeToolResult } = loadTs("lib/toolResult.ts");

  check("空内容返回空段", splitToolResults("").length === 0);
  check("无回注块 → 单段文本", (() => {
    const s = splitToolResults("普通一句话");
    return s.length === 1 && s[0].kind === "text" && s[0].text === "普通一句话";
  })());

  const one = splitToolResults(
    '<<<TOOL_RESULT name="web_search" id="c1">>>\n{"text": "搜到了 3 条"}\n<<<END_TOOL_RESULT>>>',
  );
  check("单个完整块切出 tool 段", one.length === 1 && one[0].kind === "tool");
  check("块属性：name/body/summary", one[0].name === "web_search"
    && one[0].body === '{"text": "搜到了 3 条"}'
    && one[0].summary === "搜到了 3 条"
    && one[0].truncated === false && one[0].isError === false,
    JSON.stringify(one[0]).slice(0, 120));

  const multi = splitToolResults(
    '先说一句\n<<<TOOL_RESULT name="a">>>\n{"text":"一"}\n<<<END_TOOL_RESULT>>>'
    + '\n中间一句\n<<<TOOL_RESULT name="b">>>\n{"text":"二"}\n<<<END_TOOL_RESULT>>>\n收尾',
  );
  check("多块 + 前后文本 → 段序列正确",
    multi.length === 5 && multi[0].kind === "text" && multi[1].kind === "tool"
      && multi[2].kind === "text" && multi[3].kind === "tool" && multi[4].kind === "text"
      && multi[1].summary === "一" && multi[3].summary === "二",
    multi.map((s) => s.kind).join(","));

  // 截图里的形态：块没闭合、JSON 被拦腰截断（"text":"{ 后面直接断）
  const cut = splitToolResults(
    '<<<TOOL_RESULT name="artifact_create" id="x">>>\n{"artifact_id":"a_1","title":"做","render":"artifact://a_1/render","text":"{',
  );
  check("未闭合块标记 truncated", cut.length === 1 && cut[0].kind === "tool" && cut[0].truncated === true);
  check("截断的 JSON 不抛、摘要退回原文前缀",
    cut[0].summary.includes("artifact_id") && cut[0].summary.length <= 121,
    cut[0].summary.slice(0, 60));

  const noId = splitToolResults('<<<TOOL_RESULT name="rag_query">>>\n{"error":"没绑库"}\n<<<END_TOOL_RESULT>>>');
  check("header 缺 id 不抛", noId.length === 1 && noId[0].name === "rag_query");
  check("error 字段 → isError 且摘要是错误文案",
    noId[0].isError === true && noId[0].summary === "没绑库");

  const doc = splitToolResults(
    '<<<TOOL_RESULT name="doc_generate" id="">>>\n'
    + '{"file_id":"f_1","name":"文稿.docx","doc_type":"docx","report":{"ok":true}}\n<<<END_TOOL_RESULT>>>',
  );
  check("无 text 字段 → 优先 name 字段摘要", doc[0].summary === "文稿.docx", doc[0].summary);

  const notJson = splitToolResults('<<<TOOL_RESULT name="t">>>\n纯文本结果\n<<<END_TOOL_RESULT>>>');
  check("非 JSON body → 原文摘要", notJson[0].summary === "纯文本结果");

  const keys = splitToolResults('<<<TOOL_RESULT name="t">>>\n{"a":1,"b":"二","c":[1,2]}\n<<<END_TOOL_RESULT>>>');
  check("无 text/name → 键值对摘要", keys[0].summary.includes("a=1") && keys[0].summary.includes("b=二"),
    keys[0].summary);

  check("空 body → 占位摘要", summarizeToolResult("").summary === "（空结果）");
  check("超长摘要截断加省略号", (() => {
    const s = summarizeToolResult(JSON.stringify({ text: "啊".repeat(300) }));
    return s.summary.length === 121 && s.summary.endsWith("…");
  })());
}

/** Part B：浏览器——旧会话里不再出现裸 JSON 与内部标记 */
async function testBrowser() {
  console.log("\n-- B：浏览器渲染（真实工具回注） --");
  const login = await fetch(BASE + "/api/admin/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: env("ADMIN_PASSWORD") }),
  });
  const adminCookie = (login.headers.get("set-cookie") || "").split(";")[0];
  const inv = await fetch(BASE + "/api/admin/invites", {
    method: "POST", headers: { "Content-Type": "application/json", Cookie: adminCookie },
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 30, note: "p17" }),
  }).then((r) => r.json());
  const ctx = await fetch(BASE + "/api/join", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: inv.codes[0].toLowerCase(), nickname: "p17回注" }),
  });
  const cookie = (ctx.headers.get("set-cookie") || "").split(";")[0];
  const H = { "Content-Type": "application/json", Cookie: cookie };
  const task = await fetch(BASE + "/api/tasks", {
    method: "POST", headers: H, body: JSON.stringify({ mode: "writing", goal: "p17 回注卡" }),
  }).then((r) => r.json());
  const taskId = (task.task || {}).id || "";
  check("建任务", Boolean(taskId), taskId);

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const kv = cookie.split("=");
  await context.addCookies([{ name: kv[0], value: kv[1], domain: "127.0.0.1", path: "/" }]);
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  await page.goto(BASE + "/t/" + taskId, { waitUntil: "networkidle" });
  await page.waitForSelector("[data-testid=composer-input]");
  // rag_query 在没绑知识库的任务里必然报错 → 回注一条 error 结果，够验渲染
  await page.fill("[data-testid=composer-input]",
    '#fake {"tool_on": true, "tool_name": "rag_query", "tool_input": {"query": "测试回注"}}');
  await page.click("[data-testid=send-btn]");
  await page.waitForSelector('[data-testid=tool-card][data-status="done"]', { timeout: 30000 }).catch(() => {});
  await sleep(600);

  // 刷新走「读库渲染」路径（回注消息是落库后由消息列表渲染的）
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForSelector("[data-testid=msg-user]");
  await sleep(400);

  const cards = await page.locator("[data-testid=tool-result-card]").count();
  check("工具回注渲染成摘要卡", cards >= 1, `${cards} 张`);
  const leak = await page.evaluate(() =>
    document.body.innerText.includes("<<<TOOL_RESULT")
      || document.body.innerText.includes("<<<END_TOOL_RESULT"));
  check("页面不再出现内部标记（<<<TOOL_RESULT）", !leak);
  const pureTool = await page.locator("[data-testid=msg-user][data-pure-tool]").count();
  check("纯回注消息打上 data-pure-tool（不当用户气泡）", pureTool >= 1, `${pureTool} 条`);
  const label = await page.locator("[data-testid=msg-user][data-pure-tool] .msg-label").first()
    .textContent().catch(() => "");
  check("纯回注消息标签是「工具结果」不是「你」", (label || "").includes("工具结果"), label || "");
  const summary = await page.locator("[data-testid=tool-result-card] .tool-result-summary").first()
    .textContent().catch(() => "");
  check("摘要是错误文案（rag_query 未绑库）", (summary || "").includes("没有绑定知识库"), summary || "");
  const errCls = await page.locator("[data-testid=tool-result-card]").first()
    .getAttribute("class").catch(() => "");
  check("error 结果卡带 error 样式", (errCls || "").includes("error"), errCls || "");

  // 展开能看到原始 JSON（排查用）
  await page.locator("[data-testid=tool-result-card] summary").first().click();
  await sleep(200);
  const bodyShown = await page.locator("[data-testid=tool-result-card] .tool-result-body").first()
    .isVisible().catch(() => false);
  check("展开可见原始 JSON（默认折叠）", bodyShown);
  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p17_tool_result_card.png"), fullPage: false });
  await browser.close();
}

(async () => {
  await testParser();
  await testBrowser();
  console.log(`\n== p17: ${PASS.length} 过 / ${FAIL.length} 败 ==`);
  if (FAIL.length) { console.log("失败项：", FAIL); process.exit(1); }
})().catch((e) => { console.error("P17 FAIL", e); process.exit(1); });
