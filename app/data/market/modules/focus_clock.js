/**
 * 专注时钟 —— 注册到 sidebar.footer 槽位。
 * 番茄倒计时（25 分钟专注 / 5 分钟休息）+ 实时时钟。
 * 演示 setInterval 的可逆清理：停用插件即恢复原样，不留计时器。
 */
export default {
  manifest: { id: "ui.focus-clock", name: "专注时钟", version: "0.8.0" },
  activate(ctx) {
    const { React } = ctx;
    const h = React.createElement;

    function FocusClock() {
      const [now, setNow] = React.useState(() => new Date());
      const [left, setLeft] = React.useState(25 * 60);
      const [running, setRunning] = React.useState(false);
      const [phase, setPhase] = React.useState("专注");

      React.useEffect(() => {
        const t = setInterval(() => setNow(new Date()), 1000);
        return () => clearInterval(t);
      }, []);

      React.useEffect(() => {
        if (!running) return;
        const t = setInterval(() => {
          setLeft((s) => {
            if (s <= 1) {
              setPhase((p) => (p === "专注" ? "休息" : "专注"));
              return phase === "专注" ? 5 * 60 : 25 * 60;
            }
            return s - 1;
          });
        }, 1000);
        return () => clearInterval(t);
      }, [running, phase]);

      const mm = String(Math.floor(left / 60)).padStart(2, "0");
      const ss = String(left % 60).padStart(2, "0");

      return h(
        "div",
        {
          style: {
            margin: "4px 10px 10px",
            padding: "10px 12px",
            border: "1px solid var(--line)",
            borderRadius: "var(--radius)",
            background: "var(--surface-2)",
            fontSize: "var(--fs-13)",
            color: "var(--text-2)",
          },
        },
        h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "baseline" } },
          h("span", { style: { color: "var(--muted)", fontSize: "var(--fs-12)" } }, "专注时钟 · " + phase),
          h("span", { style: { fontFamily: "var(--font-mono)" } },
            now.toLocaleTimeString("zh-CN", { hour12: false }))
        ),
        h("div", { style: { display: "flex", alignItems: "center", gap: 8, marginTop: 6 } },
          h("span", { style: { fontFamily: "var(--font-mono)", fontSize: "var(--fs-22)" } }, mm + ":" + ss),
          h("button", {
            className: "btn btn-sm",
            style: { cursor: "pointer" },
            onClick: () => setRunning((r) => !r),
          }, running ? "暂停" : "开始"),
          h("button", {
            className: "btn btn-sm btn-ghost",
            style: { cursor: "pointer" },
            onClick: () => { setRunning(false); setPhase("专注"); setLeft(25 * 60); },
          }, "重置")
        )
      );
    }

    ctx.slots.register("sidebar.footer", { id: "focus-clock", component: FocusClock, order: 20 });
    return () => {
      /* 组件卸载时 React effect 自动清理计时器；此处演示 disposer 可挂额外清理 */
    };
  },
};
