/** 路由根：boot 会话 → 401 出认证页，否则 AppShell 包住全部页面。 */
import { useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router";
import AppShell from "./components/AppShell";
import CommandPalette from "./components/CommandPalette";
import ErrorBoundary from "./components/ErrorBoundary";
import { ModalHost, ToastHost } from "./components/ui";
import { initMarketRuntime } from "./plugins/market";
import { reportClientError, useSession } from "./store/app";
import Admin from "./views/Admin";
import AdminSkills from "./views/AdminSkills";
import Auth from "./views/Auth";
import Canvas from "./views/Canvas";
import Home from "./views/Home";
import KB from "./views/KB";
import Library from "./views/Library";
import Memory from "./views/Memory";
import Plugins from "./views/Plugins";
import Settings from "./views/Settings";
import Skills from "./views/Skills";
import Task from "./views/Task";
import Tools from "./views/Tools";

export default function App() {
  const { booted, authRequired, me, boot } = useSession();
  const [bootError, setBootError] = useState("");
  const location = useLocation();
  const navigate = useNavigate();
  const isAdminPath = location.pathname.startsWith("/admin");

  useEffect(() => {
    void boot().catch((err: Error) => setBootError(err.message || "连接不上服务"));
  }, [boot]);

  // 登录成功后恢复插件市场运行时（主题换肤 + UI 模块动态加载）
  useEffect(() => {
    if (booted && me && !authRequired) void initMarketRuntime();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [booted, me?.id, authRequired]);

  // 全局命令面板开关：mod+k（P2-7）。input 聚焦时不拦截（避免吃掉输入法组合键外的正常输入）。
  const setPaletteOpen = useSession((s) => s.setPaletteOpen);
  useEffect(() => {
    if (!booted || !me || authRequired) return;
    function onKeyDown(e: KeyboardEvent) {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen(!useSession.getState().paletteOpen);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [booted, me, authRequired, setPaletteOpen]);

  if (bootError) {
    return (
      <div className="page">
        <div className="empty">
          <h4>连接不上服务</h4>
          <p>{bootError}</p>
          <button className="btn" style={{ marginTop: 12 }} onClick={() => window.location.reload()}>
            重试
          </button>
        </div>
      </div>
    );
  }

  if (!booted) {
    return (
      <div className="page" style={{ paddingTop: "20vh" }}>
        <div style={{ textAlign: "center" }} className="muted">
          <span className="spinner" style={{ width: 22, height: 22 }} />
          <p className="small faint" style={{ marginTop: 10 }}>
            正在进入…
          </p>
        </div>
      </div>
    );
  }

  if (authRequired || !me) {
    return (
      <>
        <Auth
          isAdminPath={isAdminPath}
          onDone={() => {
            // 管理员从 /admin 进来仍回管理台；访客回到登录前的深链页（丢了就回首页）
            if (isAdminPath && useSession.getState().me?.role === "admin") navigate("/admin");
            else {
              const back = location.pathname.startsWith("/admin") ? "/" : location.pathname || "/";
              navigate(back, { replace: true });
            }
          }}
        />
        <ToastHost />
      </>
    );
  }

  // 已登录的管理员从访客入口进来直接去管理台；其余照常
  if (isAdminPath && me.role !== "admin") {
    return <Navigate to="/" replace />;
  }

  return (
    <>
      <ErrorBoundary label="页面" onCaughtError={(err, info) => reportClientError(err, String(info.componentStack || ""), "route-root")}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/" element={<Home />} />
            <Route path="/t/:id" element={<Task />} />
            <Route path="/skills" element={<Skills />} />
            <Route path="/library" element={<Library />} />
            <Route path="/kb" element={<KB />} />
            <Route path="/tools" element={<Tools />} />
            <Route path="/memory" element={<Memory />} />
            <Route path="/plugins" element={<Plugins />} />
            <Route path="/canvas" element={<Canvas />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/admin" element={me.role === "admin" ? <Admin /> : <Navigate to="/" replace />} />
            <Route path="/admin/skills" element={me.role === "admin" ? <AdminSkills /> : <Navigate to="/" replace />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </ErrorBoundary>
      <CommandPalette />
      <ModalHost />
      <ToastHost />
    </>
  );
}
