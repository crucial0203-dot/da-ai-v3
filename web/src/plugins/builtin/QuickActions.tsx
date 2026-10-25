/**
 * 内置示例插件「快捷指令」—— 往首页输入框下方注入一排自定义指令按钮。
 * 证明：插件不改宿主源码，就能往 UI 挂载点加东西。
 */
import type { UIPlugin } from "../registry";

const ACTIONS = [
  { id: "polish", label: "润色这段", text: "帮我润色下面这段文字，保持原意，去掉冗余：" },
  { id: "summarize", label: "一句话总结", text: "用一句话总结下面的内容：" },
  { id: "translate", label: "译成英文", text: "把下面的内容翻译成学术英文：" },
];

function QuickActions() {
  return (
    <div className="row wrap" style={{ justifyContent: "center", gap: 6, marginTop: 8 }}
         data-testid="plugin-quick-actions">
      {ACTIONS.map((a) => (
        <button
          key={a.id}
          className="starter"
          onClick={() => window.dispatchEvent(new CustomEvent("da:fill-composer", { detail: a.text }))}
        >
          {a.label}
        </button>
      ))}
    </div>
  );
}

export const quickActions: UIPlugin = {
  manifest: {
    id: "builtin.quick-actions",
    name: "快捷指令",
    version: "0.1.0",
    description: "在首页输入框下方注入润色/总结/翻译快捷指令",
    ui: { slots: ["home.afterComposer"] },
  },
  activate(ctx) {
    ctx.slots.register("home.afterComposer", {
      id: "quick-actions",
      component: QuickActions,
      order: 10,
    });
  },
};
