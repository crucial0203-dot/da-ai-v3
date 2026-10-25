import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import App from "./App";
import { activateBuiltinPlugins } from "./plugins/builtin";
import { toast } from "./store/app";
import "./styles/tokens.css";
import "./styles/base.css";

// 插件系统 v0.1：激活内置插件（向槽位注册组件，一切注册可逆）
activateBuiltinPlugins();
// 插件桥：插件经 window 事件发提示，转发到全局 toast
window.addEventListener("da:toast", ((e: CustomEvent<{ msg: string; kind?: "info" | "error" }>) => {
  toast(e.detail.msg, e.detail.kind ?? "info");
}) as EventListener);

// 注意：不设 basename——SPA 路由挂在根路径（/、/t/:id…），
// 只有静态资源带 /dist 前缀，由服务端托管逻辑决定。
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
