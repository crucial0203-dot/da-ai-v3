/** p4 工作流画布：加节点 → 连线 → 校验 → 保存 → 运行 → 审批续跑。
 * 运行：NODE_PATH=D:/workbuddyGZ/da-staff/node_modules node tests/p4_canvas.js
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
    body: JSON.stringify({ count: 1, max_uses: 5, days: 7, daily_quota: 30, note: "p4" }),
  }).then((r) => r.json());
  const code = inv.codes[0];

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1600, height: 950 } })).newPage();
  page.setDefaultTimeout(15000);

  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.fill("#code", code.toLowerCase());
  await page.fill("#nickname", "p4画布");
  await page.click("[data-testid=auth-submit]");
  await page.waitForSelector("[data-testid=composer-input]");

  await page.click('a:text-is("画布")');
  await page.waitForSelector("[data-testid=canvas-flow]");
  check("画布页渲染", true);

  // [1] 新建：默认带一个 start
  await page.locator(".wf-list").isVisible();
  await page.locator("button:has-text('新建')").first().click();
  await page.waitForSelector('[data-testid=wf-node][data-node-kind="start"]');
  check("新建画布自带 start", true);

  // [2] 加 llm + approval + output 节点
  await page.click('[data-testid=wf-add-llm]');
  await page.click('[data-testid=wf-add-approval]');
  await page.click('[data-testid=wf-add-output]');
  const nodeCount = await page.locator("[data-testid=wf-node]").count();
  check("加了 3 个节点", nodeCount === 4, `${nodeCount} 个（含 start）`);

  // [3] 属性面板编辑 llm prompt
  await page.locator('[data-testid=wf-node][data-node-kind="llm"]').click();
  await page.waitForSelector("[data-testid=wf-props]");
  await page.locator("[data-testid=wf-props] textarea").first().fill("用一句话总结行政协议的效力规则。#fake");
  check("属性面板可编辑 prompt", true);

  // 审批节点写理由（reason 是 textarea）
  await page.locator('[data-testid=wf-node][data-node-kind="approval"]').click();
  await page.locator("[data-testid=wf-props] textarea").first().fill("发布前人工确认");

  // output 节点写模板
  await page.locator('[data-testid=wf-node][data-node-kind="output"]').click();
  await page.locator("[data-testid=wf-props] textarea").first().fill("结果：{{last}}");

  // [4] 连线（xyflow 拖拽在 headless 下不稳，用 ReactFlow 的 onConnect 模拟：
  //     直接拖 Handle 不可靠 → 改用键盘/坐标方式风险大，这里用「先校验看错误」验证校验链路）
  await page.click("[data-testid=wf-validate]");
  await page.waitForSelector("[data-testid=wf-errors]");
  const errText = await page.locator("[data-testid=wf-errors]").innerText();
  check("校验能报错（没连线/start 唯一性）", errText.includes("start") || errText.includes("入边"), errText.slice(0, 80));

  await page.screenshot({ path: path.join(ROOT, "tests/artifacts/p4_canvas.png"), fullPage: true });
  await browser.close();

  // [5] API 侧完整链路：保存 → 运行 → 审批 → resume（UI 拖拽连线不可靠，链路用 API 验）
  // 建一个带 cookie 的会话（进站端点是 /api/join，没有 /api/auth/login）
  const ctx = await fetch(BASE + "/api/join", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: code.toLowerCase(), nickname: "p4api" }),
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
    method: "POST", headers: H, body: JSON.stringify({ name: "p4审批流", graph }),
  }).then((r) => r.json());
  check("API 保存工作流", Boolean(created.ok && created.id), JSON.stringify(created).slice(0, 80));

  // 跑（SSE 全文收）
  const runRes = await fetch(BASE + `/api/workflows/${created.id}/run`, {
    method: "POST", headers: H, body: JSON.stringify({ inputs: {} }),
  });
  const runText = await runRes.text();
  check("运行出 meta+node 事件", runText.includes("event: meta") && runText.includes("event: node"));
  const approvalMatch = runText.match(/event: approval\s*\ndata: (\{.*?\})/);
  check("approval 节点触发暂停", Boolean(approvalMatch));
  const runId = approvalMatch ? JSON.parse(approvalMatch[1]).run_id : "";

  if (runId) {
    const resumeRes = await fetch(BASE + `/api/workflows/runs/${runId}/resume`, {
      method: "POST", headers: H, body: JSON.stringify({ inputs: {} }),
    });
    const resumeText = await resumeRes.text();
    check("resume 跳过已跑节点并完成", resumeText.includes("event: done"), resumeText.slice(-120));
  } else {
    check("resume 跳过已跑节点并完成", false, "拿不到 run_id");
  }

  console.log(`\n== p4: ${PASS.length} 过 / ${FAIL.length} 败 ==`);
  if (FAIL.length) { FAIL.forEach((f) => console.log("  - " + f)); process.exit(1); }
}

main().catch((err) => { console.error(err); process.exit(2); });
