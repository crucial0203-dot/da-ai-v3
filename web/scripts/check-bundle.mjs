// 构建后断言（npm run build 自动跑，pack.py 发布前再断一次）：
//   1) app/static/ui/index.html 存在
//   2) bundle 下 js+css 合计 ≤ 2.2MB（3.0MB 总包预算里给后端留 ~0.8MB）
//   3) 不允许 sourcemap（.map 直接超预算且暴露源码）
import { readdirSync, statSync, existsSync } from "node:fs";
import { join } from "node:path";

const distDir = join(import.meta.dirname, "..", "..", "app", "static", "ui");
const assetsDir = join(distDir, "bundle");
const BUDGET_BYTES = 2.2 * 1024 * 1024;

if (!existsSync(join(distDir, "index.html"))) {
  console.error("[check-bundle] FAIL: app/static/ui/index.html 不存在");
  process.exit(1);
}
if (!existsSync(assetsDir)) {
  console.error("[check-bundle] FAIL: app/static/ui/bundle 不存在");
  process.exit(1);
}

let total = 0;
const maps = [];
for (const name of readdirSync(assetsDir)) {
  if (name.endsWith(".map")) maps.push(name);
  if (name.endsWith(".js") || name.endsWith(".css")) {
    total += statSync(join(assetsDir, name)).size;
  }
}

const mb = (total / 1024 / 1024).toFixed(2);
console.log(`[check-bundle] js+css 合计 ${mb}MB / 预算 2.20MB`);

if (maps.length) {
  console.error(`[check-bundle] FAIL: 发现 sourcemap ${maps.join(", ")}`);
  process.exit(1);
}
if (total > BUDGET_BYTES) {
  console.error("[check-bundle] FAIL: 超预算，先做 manualChunks 拆分/依赖瘦身");
  process.exit(1);
}
console.log("[check-bundle] OK");
