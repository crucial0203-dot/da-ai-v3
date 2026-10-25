/** p11 画布运行时状态（P2-1/P2-2）：偏表不增删节点 + 徽标 + 置灰 + undo + 连线规则。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p11_canvas_status.js
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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Part A：connectionRules 纯函数单测（esbuild 现场转译，不进 bundle） */
async function testConnectionRules() {
  console.log("\n-- A：连线规则纯函数 --");
  const esbuild = require(path.join(ROOT, "web/node_modules/esbuild"));
  const src = path.join(ROOT, "web/src/canvas/connectionRules.ts");
  const out = esbuild.buildSync({
    entryPoints: [src],
    bundle: true,
    format: "cjs",
    write: false,
  });
  const mod = { exports: {} };
  new Function("module", "exports", out.outputFiles[0].text)(mod, mod.exports);
  const { checkConnection } = mod.exports;

  const nodes = [
    { id: "s1", kind: "start", label: "开始" },
    { id: "l1", kind: "llm", label: "写正文" },
    { id: "g1", kind: "gate", label: "汇聚" },
    { id: "o1", kind: "output", label: "输出" },
  ];
  const shape0 = { nodes, edges: [] };
  check("正常连线放行", checkConnection(shape0, "s1", "l1") === null);
  check("自环拒绝", (checkConnection(shape0, "l1", "l1") || "").includes("自身"));

  const shape1 = { nodes, edges: [{ source: "s1", target: "l1" }] };
  const cyc = checkConnection(shape1, "l1", "s1");
  check("成环拒绝", Boolean(cyc && cyc.includes("循环依赖")), cyc || "");

  const shape2 = { nodes, edges: [{ source: "s1", target: "o1" }] };
  const occ = checkConnection(shape2, "l1", "o1");
  check("单入占用拒绝", Boolean(occ && occ.includes("只能有一条入边")), occ || "");

  const shape3 = {
    nodes,
    edges: [{ source: "s1", target: "g1" }, { source: "l1", target: "g1" }],
  };
  check("gate 第二条入边放行", checkConnection(shape3, "l1", "g1") === null);
  // g1 已有两条入边，加第三条仍放行
  check("gate 第三条入边仍放行", checkConnection(shape3, "o1", "g1") === null);
}

/** Part B：浏览器验收 */
async function testBrowser() {
  console.log("\n-- B：画布运行时状态 --");
  const login = await fetch(BASE + "/api/admin/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: env("ADMIN_PASSWORD") }),
  });
  const adminCookie = (login.headers.get("set-cookie") || "").split(";")[0];
  const inv = await fetch(BASE + "/api/admin/invites", {
    method: "POST", headers: { "Content-Type": "application/json", Cookie: adminCookie },
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 30, note: "p11" }),
  }).then((r) => r.json());
  const code = inv.codes[0];

  // API 建一个带 approval 的工作流
  const ctx = await fetch(BASE + "/api/join", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: code.toLowerCase(), nickname: "p11api" }),
  });
  const cookie = (ctx.headers.get("set-cookie") || "").split(";")[0];
  const H = { "Content-Type": "application/json", Cookie: cookie };
  const graph = {
    nodes: [
      { id: "s1", type: "start", position: { x: 0, y: 0 }, data: { label: "开始" } },
      { id: "l1", type: "llm", position: { x: 0, y: 120 }, data: { label: "写正文", prompt: "总结行政协议效力。#fake" } },
      { id: "a1", type: "approval", position: { x: 0, y: 240 }, data: { label: "确认", reason: "发布前人工确认" } },
      { id: "o1", type: "output", position: { x: 0, y: 360 }, data: { label: "输出", text: "{{last}}" } },
    ],
    edges: [
      { id: "e1", source: "s1", target: "l1" },
      { id: "e2", source: "l1", target: "a1" },
      { id: "e3", source: "a1", target: "o1" },
    ],
  };
  const created = await fetch(BASE + "/api/workflows", {
    method: "POST", headers: H, body: JSON.stringify({ name: "p11审批流", graph }),
  }).then((r) => r.json());
  if (!created.ok) throw new Error("建工作流失败：" + JSON.stringify(created).slice(0, 120));

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  // 工作流按用户隔离：浏览器必须复用 API 建图的那个会话（注入 sid cookie）
  const sid = cookie.split("=")[1];
  const context = await browser.newContext({ viewport: { width: 1600, height: 950 } });
  await context.addCookies([{ name: cookie.split("=")[0], value: sid, domain: "127.0.0.1", path: "/" }]);
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  await page.goto(BASE + "/canvas", { waitUntil: "networkidle" });
  await page.waitForSelector("[data-testid=canvas-flow]");

  // 载入工作流
  await page.locator(".wf-item", { hasText: "p11审批流" }).click();
  await sleep(600);
  const n0 = await page.locator("[data-testid=wf-node]").count();
  check("载入后 4 个节点", n0 === 4, `${n0} 个`);

  // 运行
  await page.click("[data-testid=wf-run]");
  await page.waitForSelector('[data-testid=wf-badge][data-badge-status="done"]', { timeout: 20000 });
  await page.waitForSelector("[data-testid=wf-approve]", { timeout: 20000 });

  // 审批暂停点：节点数不变；未走到（o1）置灰；s1/l1 有完成徽标；a1 待确认
  const nPause = await page.locator("[data-testid=wf-node]").count();
  check("审批暂停时节点数不变（运行不增删节点）", nPause === 4, `${nPause} 个`);
  const dimmedIds = await page.evaluate(() =>
    [...document.querySelectorAll(".wf-node._dimmed")].map((el) => {
      // xyflow 节点容器带 data-id
      const wrap = el.closest(".react-flow__node");
      return wrap ? wrap.getAttribute("data-id") : "";
    }),
  );
  check("未走到节点置灰（只有 o1；a1 已标待确认）",
    dimmedIds.join(",") === "o1", dimmedIds.join(","));
  const doneBadges = await page.locator('[data-testid=wf-badge][data-badge-status="done"]').count();
  check("已完成节点有徽标（s1+l1）", doneBadges === 2, `${doneBadges} 个`);
  const waitBadge = await page.locator('[data-testid=wf-badge][data-badge-status="waiting"]').count();
  check("审批节点显示待确认徽标", waitBadge === 1, `${waitBadge} 个`);
  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p11_paused.png"), fullPage: true });

  // 通过审批 → 跑完
  await page.click("[data-testid=wf-approve]");
  await page.waitForSelector("[data-testid=wf-log]", { timeout: 30000 });
  await page.waitForFunction(
    () => [...document.querySelectorAll('[data-testid=wf-badge][data-badge-status="done"]')].length === 4,
    { timeout: 30000 },
  );
  const nEnd = await page.locator("[data-testid=wf-node]").count();
  check("跑完后节点数仍为 4", nEnd === 4, `${nEnd} 个`);
  const dimmedEnd = await page.locator(".wf-node._dimmed").count();
  check("跑完后无置灰节点", dimmedEnd === 0, `${dimmedEnd} 个`);
  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p11_done.png"), fullPage: true });

  // undo / redo：加节点 → Ctrl+Z 回退 → Ctrl+Shift+Z 重做
  await page.click("[data-testid=wf-add-rag]");
  await sleep(500); // 等 undo 防抖落栈
  const nAdd = await page.locator("[data-testid=wf-node]").count();
  check("加了 rag 节点（5 个）", nAdd === 5, `${nAdd} 个`);
  await page.click("[data-testid=canvas-flow] .react-flow__pane");
  await page.keyboard.press("Control+z");
  await sleep(300);
  const nUndo = await page.locator("[data-testid=wf-node]").count();
  check("Ctrl+Z 撤销回 4 个", nUndo === 4, `${nUndo} 个`);
  await page.keyboard.press("Control+Shift+z");
  await sleep(300);
  const nRedo = await page.locator("[data-testid=wf-node]").count();
  check("Ctrl+Shift+Z 重做回 5 个", nRedo === 5, `${nRedo} 个`);

  await browser.close();
}

async function main() {
  await testConnectionRules();
  await testBrowser();
  console.log(`\n== p11: ${PASS.length} 过 / ${FAIL.length} 败 ==`);
  if (FAIL.length) { FAIL.forEach((f) => console.log("  - " + f)); process.exit(1); }
}

main().catch((err) => { console.error(err); process.exit(2); });
