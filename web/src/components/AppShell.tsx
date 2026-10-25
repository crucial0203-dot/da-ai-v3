/** 应用壳：侧栏（品牌/搜索/导航/历史/用量+主题）+ 顶栏 + 路由出口。 */
import { useCallback, useEffect, useMemo, useState, type UIEvent } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router";
import { formatTime } from "../lib/format";
import QuotaBanner from "./QuotaBanner";
import { useSession, type Theme } from "../store/app";
import { Slot } from "../plugins/slots";

const NAV = [
  { to: "/", label: "对话", end: true, key: "home" },
  { to: "/library", label: "库", key: "library" },
  { to: "/kb", label: "知识库", key: "kb" },
  { to: "/skills", label: "技能", key: "skills" },
  { to: "/tools", label: "工具", key: "tools" },
  { to: "/plugins", label: "插件市场", key: "plugins" },
  { to: "/memory", label: "记忆", key: "memory" },
  { to: "/canvas", label: "画布", key: "canvas" },
  { to: "/settings", label: "设置", key: "settings" },
];

/** 侧栏历史每页条数：首屏一页，滚到底再加载一页。 */
const PAGE = 50;

/** 今日用量： normal/tight/exhausted 三态 + 重置倒计时（组件在 QuotaBanner）。
 *  根节点 data-testid="usage-bar" 由 QuotaBanner 保留，验收脚本靠它找。 */
function UsageBar() {
  const me = useSession((s) => s.me);
  const usage = useSession((s) => s.quota);
  const refreshUsage = useSession((s) => s.refreshUsage);

  // 登录态变化时拉一次；对话结束后由 Task 页的 onEnd 再刷新（refreshUsage）
  useEffect(() => {
    if (me?.role === "visitor") void refreshUsage();
  }, [me, refreshUsage]);

  if (!usage || !usage.quota) return null;
  return <QuotaBanner used={usage.used} quota={usage.quota} />;
}

function ThemeSwitch() {
  const { theme, setTheme } = useSession();
  const opts: Array<[Theme, string]> = [
    ["light", "浅色"],
    ["dark", "深色"],
    ["oled", "纯黑"],
  ];
  return (
    <div className="row tiny" style={{ gap: 4 }}>
      {opts.map(([t, label]) => (
        <button
          key={t}
          className="filter-chip"
          style={{ padding: "2px 9px", fontSize: 11 }}
          data-active={theme === t}
          onClick={() => setTheme(t)}
          title={`${label}主题`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

export default function AppShell() {
  const { me, tasks, meta, refreshTasks } = useSession();
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem("da_sidebar_collapsed") === "1";
    } catch {
      return false;
    }
  });
  const [filter, setFilter] = useState("");
  const location = useLocation();
  const navigate = useNavigate();
  const activeTaskId = location.pathname.startsWith("/t/") ? location.pathname.slice(3) : "";

  useEffect(() => {
    try {
      localStorage.setItem("da_sidebar_collapsed", collapsed ? "1" : "");
    } catch {
      /* ignore */
    }
  }, [collapsed]);

  // 路由变化时刷新历史（任务改名/新建后侧栏跟上）
  useEffect(() => {
    if (location.pathname.startsWith("/t/")) void refreshTasks();
  }, [location.pathname, refreshTasks]);

  // 筛选后的全量列表（不再 .slice(0,30) 硬截断——老会话直接消失，
  // 用户以为数据丢了）。改成首屏 50 条 + 滚到底增量 +50。
  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return tasks.filter((t) => !q || t.title.toLowerCase().includes(q));
  }, [tasks, filter]);

  const [shown, setShown] = useState(PAGE);
  // 换筛选词时重置可视窗口，否则还挂着上一个关键词的 100+
  useEffect(() => {
    setShown(PAGE);
  }, [filter]);

  const history = filtered.slice(0, shown);
  const hasMore = shown < filtered.length;

  const onHistoryScroll = useCallback(
    (e: UIEvent<HTMLDivElement>) => {
      const el = e.currentTarget;
      if (el.scrollHeight - el.scrollTop - el.clientHeight > 200) return;
      // 本地内存数据，没有异步 loading；去重就是「还有更多才推进」
      setShown((n) => (n < filtered.length ? n + PAGE : n));
    },
    [filtered.length],
  );

  const isAdmin = me?.role === "admin";
  const title = location.pathname.startsWith("/t/")
    ? "会话"
    : NAV.find((n) => n.to === location.pathname)?.label || "对话";

  return (
    <div className={`shell ${collapsed ? "collapsed" : ""}`}>
      <aside className="sidebar" data-testid="sidebar">
        <div className="side-head">
          <Link className="brand" to="/">
            <span className="brand-mark" />
            <span>
              <span className="brand-name">{meta?.app || "DA的AI"}</span>
              <span className="brand-sub">工作台</span>
            </span>
          </Link>
          <button className="btn btn-ghost btn-icon btn-sm" title="收起侧栏" onClick={() => setCollapsed(true)}>
            ⟨
          </button>
        </div>
        <div className="side-search">
          <input type="search" placeholder="搜会话…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        </div>
        <nav className="side-nav">
          {NAV.map((n) => (
            <NavLink key={n.key} to={n.to} end={n.end} className={({ isActive }) => `side-item ${isActive ? "active" : ""}`}>
              {n.label}
            </NavLink>
          ))}
          {isAdmin && (
            <NavLink to="/admin" className={({ isActive }) => `side-item ${isActive ? "active" : ""}`}>
              管理台
            </NavLink>
          )}
          <button
            className="side-item"
            style={{ border: "none", background: "none", cursor: "pointer", textAlign: "left" }}
            onClick={() => {
              navigate("/");
              setTimeout(() => document.querySelector<HTMLTextAreaElement>("[data-testid=composer-input]")?.focus(), 60);
            }}
          >
            ＋ 新对话
          </button>
        </nav>
        <div className="side-history-label">最近会话</div>
        <div
          className="side-history-list"
          data-testid="history-list"
          onScroll={onHistoryScroll}
        >
          {filtered.length === 0 ? (
            <div style={{ padding: "0 10px" }}>
              <p className="tiny faint" style={{ margin: 0 }}>
                {filter ? "没有匹配的会话" : "还没有会话，去发第一条消息。"}
              </p>
              {filter && (
                <button
                  className="btn btn-ghost btn-sm"
                  style={{ marginTop: 6, padding: "2px 8px" }}
                  data-testid="history-clear-filter"
                  onClick={() => setFilter("")}
                >
                  清空筛选
                </button>
              )}
            </div>
          ) : (
            history.map((t) => (
              <Link
                key={t.id}
                to={`/t/${t.id}`}
                className={`side-hist ${activeTaskId === t.id ? "active" : ""}`}
                title={t.title}
              >
                <span className="truncate grow">{t.title}</span>
                <span className="tiny faint">{formatTime(t.updated_at)}</span>
              </Link>
            ))
          )}
          {hasMore && (
            <p className="tiny faint" style={{ padding: "6px 10px", margin: 0 }} data-testid="history-more">
              还有 {filtered.length - history.length} 条，滚动到底部加载
            </p>
          )}
        </div>
        {/* 插件挂载点：sidebar.footer */}
        <Slot name="sidebar.footer" />
        <ThemeSwitch />
        <UsageBar />
      </aside>

      <div className="main">
        <header className="topbar">
          {collapsed && (
            <button className="btn btn-ghost btn-icon btn-sm" title="展开侧栏" onClick={() => setCollapsed(false)}>
              ☰
            </button>
          )}
          <span className="topbar-title">{title}</span>
          <span className="grow" />
          <Link className="avatar btn btn-ghost btn-sm" to="/settings" title={me?.nickname || ""}>
            {(me?.nickname || "D").slice(0, 1)}
          </Link>
        </header>
        <Outlet />
      </div>
    </div>
  );
}
