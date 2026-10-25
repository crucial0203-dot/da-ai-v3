/** p16 批次 2 前端验收（S2-2 连线磁吸 / S2-3 部分 JSON / S2-6 市场卡 / S2-7 自动布局）。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p16_batch2.js
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

/** 把 TS 现场转译成 CJS 后 require（纯函数单测，不进 bundle）。 */
function loadTs(rel) {
  const out = esbuild.buildSync({
    entryPoints: [path.join(ROOT, "web/src", rel)],
    bundle: true, format: "cjs", write: false,
  });
  const mod = { exports: {} };
  new Function("module", "exports", out.outputFiles[0].text)(mod, mod.exports);
  return mod.exports;
}

/** Part A：computeAutoLayout 纯函数 */
async function testAutoLayoutUnit() {
  console.log("\n-- A：自动布局纯函数 --");
  const { computeAutoLayout } = loadTs("canvas/autoLayout.ts");
  const chain = [
    { id: "s1", position: { x: 900, y: 500 }, data: { kind: "start" } },
    { id: "l1", position: { x: 10, y: 10 }, data: { kind: "llm" } },
    { id: "o1", position: { x: 400, y: 60 }, data: { kind: "output" } },
  ];
  const edges = [{ source: "s1", target: "l1" }, { source: "l1", target: "o1" }];
  const laid = computeAutoLayout(chain, edges);
  const byId = Object.fromEntries(laid.map((n) => [n.id, n.position]));
  check("链式图按层排（y 沿流向递增）",
    byId.s1.y < byId.l1.y && byId.l1.y < byId.o1.y,
    `s1=${Math.round(byId.s1.y)} l1=${Math.round(byId.l1.y)} o1=${Math.round(byId.o1.y)}`);
  check("链式图同列（x 对齐）",
    Math.abs(byId.s1.x - byId.l1.x) < 1 && Math.abs(byId.l1.x - byId.o1.x) < 1,
    `${Math.round(byId.s1.x)}/${Math.round(byId.l1.x)}/${Math.round(byId.o1.x)}`);
  check("只改 position（id/data 不动）",
    laid[0].id === "s1" && laid[0].data.kind === "start" && laid.length === 3);
  check("输入不被修改（不可变）", chain[0].position.x === 900);

  const fork = computeAutoLayout(
    [
      { id: "s", position: { x: 0, y: 0 } },
      { id: "a", position: { x: 0, y: 0 } },
      { id: "b", position: { x: 0, y: 0 } },
      { id: "g", position: { x: 0, y: 0 } },
    ],
    [{ source: "s", target: "a" }, { source: "s", target: "b" },
     { source: "a", target: "g" }, { source: "b", target: "g" }],
  );
  const f = Object.fromEntries(fork.map((n) => [n.id, n.position]));
  check("分支同层不重叠（a/b 同 y 不同 x）",
    Math.abs(f.a.y - f.b.y) < 1 && Math.abs(f.a.x - f.b.x) > 200,
    `a.x=${Math.round(f.a.x)} b.x=${Math.round(f.b.x)}`);
  check("汇聚点在下游层", f.g.y > f.a.y && f.g.y > f.b.y);

  const dirty = computeAutoLayout(
    [{ id: "x", position: { x: 0, y: 0 } }],
    [{ source: "x", target: "ghost" }, { source: "ghost2", target: "x" }],
  );
  check("脏边（未知节点）被忽略不抛", dirty.length === 1 && dirty[0].id === "x");
  check("空图返回空", computeAutoLayout([], []).length === 0);
}

/** Part A2：连线规则纯函数。浏览器里非法拖拽被 isValidConnection 直接丢弃、
 * onConnect 不触发（xyflow onPointerUp 源码：isValid 为假不调 onConnect），
 * 所以 toast 文案只能在这里验。 */
async function testConnectionRules() {
  console.log("\n-- A2：连线规则纯函数 --");
  const { checkConnection, MULTI_IN_KINDS } = loadTs("canvas/connectionRules.ts");
  const chain = {
    nodes: [
      { id: "s1", kind: "start", label: "开始" },
      { id: "l1", kind: "llm", label: "写正文" },
      { id: "g1", kind: "gate", label: "逻辑门" },
      { id: "o1", kind: "output", label: "输出" },
    ],
    edges: [
      { source: "s1", target: "l1" },
      { source: "l1", target: "g1" },
      { source: "g1", target: "o1" },
    ],
  };
  check("自环被拦", checkConnection(chain, "l1", "l1") === "不能连接节点自身");
  check("未知节点被拦", (checkConnection(chain, "l1", "ghost") || "").includes("不存在"));
  const cyc = checkConnection(chain, "g1", "l1") || "";
  check("成环被拦且文案带「循环依赖」", cyc.includes("循环依赖"), cyc);
  const occ = checkConnection(chain, "s1", "o1") || "";
  check("单入占用被拦且文案提示唯一入边", occ.includes("只能有一条入边"), occ);
  check("gate 节点多入豁免", checkConnection(chain, "s1", "g1") === null);
  check("MULTI_IN_KINDS 只含 gate", MULTI_IN_KINDS.has("gate") && MULTI_IN_KINDS.size === 1);
  const open = { nodes: chain.nodes, edges: [{ source: "s1", target: "l1" }] };
  check("空闲落点放行", checkConnection(open, "l1", "g1") === null);
}

/** 登录拿 cookie（邀请码加入）。
 *
 * /api/join 有真实限流（deps.limit_join：同 IP 5 分钟 12 次）——一次跑里 join
 * 三次、加上反复重跑，会把自己撞进 429（表现为「请先用邀请码进入」，
 * 因为拿不到 set-cookie）。所以：整个套件共用一个会话，且 429 时退避重试。
 */
let SESSION = null;
async function getSession() {
  if (SESSION) return SESSION;
  const login = await fetch(BASE + "/api/admin/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: env("ADMIN_PASSWORD") }),
  });
  const adminCookie = (login.headers.get("set-cookie") || "").split(";")[0];
  for (let attempt = 1; attempt <= 3; attempt++) {
    const inv = await fetch(BASE + "/api/admin/invites", {
      method: "POST", headers: { "Content-Type": "application/json", Cookie: adminCookie },
      body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 30, note: "p16" }),
    }).then((r) => r.json());
    const ctx = await fetch(BASE + "/api/join", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: inv.codes[0].toLowerCase(), nickname: "p16甲" }),
    });
    const cookie = (ctx.headers.get("set-cookie") || "").split(";")[0];
    if (cookie) {
      SESSION = cookie;
      return cookie;
    }
    console.log(`  .. join 被限流（第 ${attempt} 次），退避 70s`);
    await sleep(70000);
  }
  throw new Error("连续三次 join 都被限流");
}

/** 建工作流 + 拿浏览器会话（复用 p11 的 API 建图 → cookie 注入套路）。 */
async function setupCanvas(page, graph, name) {
  const cookie = await getSession();
  const H = { "Content-Type": "application/json", Cookie: cookie };
  const created = await fetch(BASE + "/api/workflows", {
    method: "POST", headers: H, body: JSON.stringify({ name, graph }),
  }).then((r) => r.json());
  if (!created.ok) throw new Error("建工作流失败：" + JSON.stringify(created).slice(0, 120));
  return cookie;
}

/** 读画布上所有节点的 data-id + transform（位置）。 */
async function nodeBoxes(page) {
  return page.evaluate(() =>
    [...document.querySelectorAll(".react-flow__node")].map((el) => {
      const m = /translate\(([-\d.]+)px,\s*([-\d.]+)px\)/.exec(el.style.transform || "");
      return { id: el.getAttribute("data-id"), x: m ? Number(m[1]) : NaN, y: m ? Number(m[2]) : NaN };
    }),
  );
}

async function testCanvasBrowser() {
  console.log("\n-- B：连线磁吸 + 删边 + 自动布局（浏览器） --");
  // 4 节点链 s1→l1→g1→o1：g1 是逻辑门（唯一多入豁免类型），顺带在浏览器里
  // 验 gate 特例；非 start 节点都有入边，服务端存盘校验一次过。
  const graph = {
    nodes: [
      { id: "s1", type: "start", position: { x: 40, y: 40 }, data: { label: "开始" } },
      { id: "l1", type: "llm", position: { x: 40, y: 260 }, data: { label: "写正文", prompt: "写点什么" } },
      { id: "g1", type: "gate", position: { x: 40, y: 480 }, data: { label: "逻辑门", op: "AND" } },
      { id: "o1", type: "output", position: { x: 420, y: 480 }, data: { label: "输出", text: "{{last}}" } },
    ],
    edges: [
      { id: "e1", source: "s1", target: "l1" },
      { id: "e2", source: "l1", target: "g1" },
      { id: "e3", source: "g1", target: "o1" },
    ],
  };
  const cookie = await setupCanvas(null, graph, "p16磁吸流");
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1600, height: 950 } });
  const kv = cookie.split("=");
  await context.addCookies([{ name: kv[0], value: kv[1], domain: "127.0.0.1", path: "/" }]);
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  await page.goto(BASE + "/canvas", { waitUntil: "networkidle" });
  await page.waitForSelector("[data-testid=canvas-flow]");
  await page.locator(".wf-item", { hasText: "p16磁吸流" }).click();
  await sleep(600);

  const edgeCount = () => page.locator(".react-flow__edge").count();
  check("载入后 4 节点 3 边", (await page.locator("[data-testid=wf-node]").count()) === 4
    && (await edgeCount()) === 3, `edges=${await edgeCount()}`);

  /** 从 fromSel 拖到 toSel；返回松手前的连线呈现（shown=是否出现走向线）。 */
  async function dragTo(fromSel, toSel) {
    const from = await page.locator(fromSel).boundingBox();
    const to = await page.locator(toSel).boundingBox();
    await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
    await page.mouse.down();
    await page.mouse.move(to.x + to.width / 2, to.y + to.height / 2, { steps: 12 });
    await sleep(250); // 等 isValidConnection 结算
    const line = page.locator("[data-testid=wf-connection-line]");
    const shown = (await line.count()) === 1;
    const status = shown ? await line.getAttribute("data-connection-status") : null;
    await page.mouse.up();
    await sleep(300);
    return { shown, status };
  }
  const G1_OUT = '.react-flow__node[data-id="g1"] .react-flow__handle-bottom';
  const L1_IN = '.react-flow__node[data-id="l1"] .react-flow__handle-top';
  const G1_IN = '.react-flow__node[data-id="g1"] .react-flow__handle-top';
  const O1_IN = '.react-flow__node[data-id="o1"] .react-flow__handle-top';
  const S1_OUT = '.react-flow__node[data-id="s1"] .react-flow__handle-bottom';

  // ① 成环：g1 → l1（l1→g1 已在）→ 实时 invalid，松手不建边、不弹 toast
  const cyc = await dragTo(G1_OUT, L1_IN);
  check("拖拽中出现连线走向线", cyc.shown);
  check("成环落点连线实时变 invalid", cyc.status === "invalid", String(cyc.status));
  check("成环松手不建边", (await edgeCount()) === 3, `edges=${await edgeCount()}`);

  // ② 删边腾落点：点选 e3（g1→o1）→ Backspace（xyflow v12 默认删除键，
  //    源码 deleteKeyCode = 'Backspace'，Delete 不是默认键）
  const edgePath = page.locator('.react-flow__edge[data-id="e3"] .react-flow__edge-path');
  const eb = await edgePath.boundingBox();
  await page.mouse.click(eb.x + eb.width / 2, eb.y + eb.height / 2);
  await sleep(200);
  check("点选边成功（可删除）", (await page.locator(".react-flow__edge.selected").count()) === 1);
  await page.keyboard.press("Backspace");
  await sleep(300);
  check("Backspace 删掉选中的边", (await edgeCount()) === 2, `edges=${await edgeCount()}`);

  // ③ 合法拖拽：g1 → o1（o1 已空闲）→ valid 并建成
  const ok = await dragTo(G1_OUT, O1_IN);
  check("合法落点连线为 valid（实时反馈）", ok.status === "valid", String(ok.status));
  check("合法落点松手建成边", (await edgeCount()) === 3, `edges=${await edgeCount()}`);

  // ④ 单入占用：g1 → o1 再来一次 → invalid，不建边
  const occ = await dragTo(G1_OUT, O1_IN);
  check("单入占用落点也实时 invalid", occ.status === "invalid", String(occ.status));
  check("占用落点不建边", (await edgeCount()) === 3, `edges=${await edgeCount()}`);

  // ⑤ gate 多入豁免：s1 → g1（g1 已有 l1 的入边）→ valid 并建成
  const gate = await dragTo(S1_OUT, G1_IN);
  check("gate 多入豁免：已有入边仍判 valid", gate.status === "valid", String(gate.status));
  check("gate 多入松手建成第二条入边", (await edgeCount()) === 4, `edges=${await edgeCount()}`);
  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p16_snap_feedback.png"), fullPage: true });

  // ---- S2-7 自动布局 ----
  const before = await nodeBoxes(page);
  await page.click("[data-testid=wf-autolayout]");
  await sleep(500);
  const after = await nodeBoxes(page);
  const moved = before.filter((b) => {
    const a = after.find((n) => n.id === b.id);
    return a && (Math.abs(a.x - b.x) > 5 || (Math.abs(a.y - b.y) > 5));
  }).length;
  check("整理布局移动了节点", moved >= 2, `moved=${moved}`);
  const pos = Object.fromEntries(after.map((n) => [n.id, n]));
  check("整理后 start 在最上层", pos.s1.y < pos.l1.y && pos.s1.y < pos.g1.y && pos.s1.y < pos.o1.y,
    `s1=${Math.round(pos.s1.y)} l1=${Math.round(pos.l1.y)} g1=${Math.round(pos.g1.y)} o1=${Math.round(pos.o1.y)}`);
  check("整理后 output 在最下游", pos.o1.y > pos.l1.y && pos.o1.y > pos.g1.y,
    `l1=${Math.round(pos.l1.y)} g1=${Math.round(pos.g1.y)} o1=${Math.round(pos.o1.y)}`);
  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p16_autolayout.png"), fullPage: true });

  // 撤销回到整理前（useUndo 要求栈里至少两帧；等过 300ms 防抖再按）
  await page.keyboard.press("Control+z");
  await sleep(400);
  const undone = await nodeBoxes(page);
  const backClose = before.every((b) => {
    const u = undone.find((n) => n.id === b.id);
    return u && Math.abs(u.x - b.x) < 2 && Math.abs(u.y - b.y) < 2;
  });
  check("整理布局可撤销（位置还原）", backClose,
    JSON.stringify(undone.map((n) => [n.id, Math.round(n.x), Math.round(n.y)])));

  await browser.close();
}

async function testMarket() {
  console.log("\n-- C：市场 218 卡 + content-visibility --");
  const cookie = await getSession();
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const kv = cookie.split("=");
  await context.addCookies([{ name: kv[0], value: kv[1], domain: "127.0.0.1", path: "/" }]);
  const page = await context.newPage();
  page.setDefaultTimeout(20000);
  await page.goto(BASE + "/plugins", { waitUntil: "networkidle" });
  await page.waitForSelector('[data-testid="plugins-page"]');
  await page.click('[data-testid="market-tab-dsh"]');
  await page.waitForSelector('[data-testid="dsh-card"]');
  await sleep(400);

  const count = await page.locator("[data-testid=dsh-card]").count();
  check("200 张卡仍在 DOM（窗口化不改计数）", count === 200, `${count} 张`);
  const cv = await page.evaluate(() => {
    const el = document.querySelector("[data-testid=dsh-card]:last-of-type") || document.querySelector("[data-testid=dsh-card]");
    return el ? getComputedStyle(el).contentVisibility : "none";
  });
  check("卡片启用 content-visibility（离屏跳过布局绘制）", cv === "auto", String(cv));
  // 滚到底再数：离屏卡不卸载
  await page.evaluate(() => { window.scrollTo(0, document.body.scrollHeight); });
  await sleep(600);
  const count2 = await page.locator("[data-testid=dsh-card]").count();
  check("滚到底后仍是 200 张", count2 === 200, `${count2} 张`);
  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p16_market_bottom.png"), fullPage: false });
  await browser.close();
}

async function testStreamingArgs() {
  console.log("\n-- D：流式部分参数（浏览器） --");
  const cookie = await getSession();
  const H = { "Content-Type": "application/json", Cookie: cookie };
  const task = await fetch(BASE + "/api/tasks", {
    method: "POST", headers: H, body: JSON.stringify({ mode: "writing", goal: "p16 部分参数" }),
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
  // fence_char_parts=20 逐字写围栏 + fence_ms=80 拖慢到肉眼/探针可观测
  await page.fill("[data-testid=composer-input]",
    '#fake {"tool_on": true, "fence_char_parts": 20, "fence_ms": 80, "tool_name": "memory_query", "tool_input": {"query": "用户的偏好是什么"}}');
  await page.click("[data-testid=send-btn]");

  // 流式期间：至少出现过一次「参数写入中」的工具卡
  let sawWriting = false;
  let sawPartial = false;
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const state = await page.evaluate(() => {
      const writing = document.querySelectorAll('[data-testid=tool-card][data-args-writing="true"]').length;
      const cards = [...document.querySelectorAll("[data-testid=tool-card]")];
      const partial = cards.some((c) => {
        const pre = c.querySelector(".tool-input");
        return pre && /用户的偏(?!好是什么")/.test(pre.textContent || "");
      });
      return { writing, partial, done: Boolean(document.querySelector("[data-testid=tool-card][data-status=done]")) };
    });
    if (state.writing > 0) sawWriting = true;
    if (state.partial) sawPartial = true;
    if (state.done && !state.writing) break;
    await sleep(60);
  }
  check("出现过「参数写入中」状态卡", sawWriting);
  check("出现过半截 query 的参数预览（fix-json 生效）", sawPartial);
  await page.waitForSelector('[data-testid=tool-card][data-status="done"]', { timeout: 20000 }).catch(() => {});
  await sleep(400);
  const finalInput = await page.evaluate(() => {
    const pre = document.querySelector('[data-testid=tool-card][data-status="done"] .tool-input');
    return pre ? pre.textContent || "" : "";
  });
  check("完成后参数完整", finalInput.includes("用户的偏好是什么"), finalInput.slice(0, 90));
  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p16_streaming_args.png"), fullPage: false });
  await browser.close();
}

(async () => {
  await testAutoLayoutUnit();
  await testConnectionRules();
  await testCanvasBrowser();
  await testMarket();
  await testStreamingArgs();
  console.log(`\n== p16: ${PASS.length} 过 / ${FAIL.length} 败 ==`);
  if (FAIL.length) { console.log("失败项：", FAIL); process.exit(1); }
})().catch((e) => { console.error("P16 FAIL", e); process.exit(1); });
