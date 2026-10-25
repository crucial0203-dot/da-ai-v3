/**
 * 内置示例插件「插件面板」—— 往设置页注入已激活插件清单。
 * 插件系统自证：连「插件管理界面」本身也是插件贡献的。
 */
import type { UIPlugin } from "../registry";
import { listActivePlugins } from "../registry";
import { SLOTS } from "../slots";

function PluginPanel() {
  const plugins = listActivePlugins();
  return (
    <section className="card" data-testid="plugin-panel" style={{ marginTop: 12 }}>
      <h3 style={{ marginTop: 0 }}>插件</h3>
      <p className="muted" style={{ marginTop: 0 }}>
        本面板由插件注入（settings.plugins 槽位）。插件可向声明过的挂载点贡献组件、向后端注册工具。
      </p>
      {plugins.map((m) => (
        <div key={m.id} className="row-between" style={{ padding: "6px 0", borderTop: "1px solid var(--border)" }}>
          <div>
            <strong>{m.name}</strong>
            <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>
              {m.id} · v{m.version}
            </span>
            {m.description && <div className="muted" style={{ fontSize: 12 }}>{m.description}</div>}
          </div>
          <div className="row" style={{ gap: 4 }}>
            {(m.ui?.slots ?? []).map((s) => (
              <span key={s} className="badge" title={SLOTS[s]?.desc || ""}>{s}</span>
            ))}
          </div>
        </div>
      ))}
    </section>
  );
}

export const pluginPanel: UIPlugin = {
  manifest: {
    id: "builtin.plugin-panel",
    name: "插件面板",
    version: "0.1.0",
    description: "在设置页展示已激活插件及其挂载点",
    ui: { slots: ["settings.plugins"] },
  },
  activate(ctx) {
    ctx.slots.register("settings.plugins", {
      id: "plugin-panel",
      component: PluginPanel,
      order: 10,
    });
  },
};
