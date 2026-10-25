/**
 * 内置示例插件「会话统计」—— 往会话头部注入统计按钮。
 * 证明：插件能拿到宿主传入的上下文（messages）并注入交互。
 */
import type { UIPlugin } from "../registry";

interface StatsProps {
  messages?: Array<{ role: string; content?: string }>;
}

function ChatStatsButton({ messages }: StatsProps) {
  const list = messages ?? [];
  const chars = list.reduce((n, m) => n + (m.content?.length ?? 0), 0);
  return (
    <button
      className="btn btn-ghost btn-sm"
      data-testid="plugin-chat-stats"
      title="由插件注入"
      onClick={() => {
        window.dispatchEvent(
          new CustomEvent("da:toast", {
            detail: { msg: `本会话 ${list.length} 条消息 · 约 ${chars} 字`, kind: "info" },
          }),
        );
      }}
    >
      统计
    </button>
  );
}

export const chatStats: UIPlugin = {
  manifest: {
    id: "builtin.chat-stats",
    name: "会话统计",
    version: "0.1.0",
    description: "在会话头部注入消息数与字数统计",
    ui: { slots: ["chat.header.actions"] },
  },
  activate(ctx) {
    ctx.slots.register("chat.header.actions", {
      id: "chat-stats",
      component: ChatStatsButton,
      order: 10,
    });
  },
};
