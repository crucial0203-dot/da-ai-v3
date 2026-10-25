import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// 产物必须落 app/static/ui/ 并随包发布：沙箱运行时没有 node，不能现场构建。
// 目录名刻意避开 dist/build/out——发布上传器会按名字把「构建产物目录」剔出包，
// 2026-10-23 实测 app/static/dist/ 被整个剔掉导致线上 / 500。
// base='/ui/' 让资源 URL 变成 /ui/bundle/x.js，命中现有静态托管逻辑，零改后端路由。
export default defineConfig({
  base: "/ui/",
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../app/static/ui",
    assetsDir: "bundle",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      // 本地开发：后端起在 8020（run.py 默认端口），前端 dev server 同源转发
      "/api": { target: "http://127.0.0.1:8020", changeOrigin: true },
    },
  },
});
