/**
 * 思考过程折叠块。
 *
 * 三条行为规则（对应「默认关 / 运行中自动展开 / 有正文自动收起」）：
 *   ① 默认收起——思考过程对大多数用户是噪音，摊开会把回答挤到屏幕外；
 *   ② 模型还在吐思考、正文还没开始时自动展开，让等待有事可看；
 *   ③ 正文一开始到达就自动收起，把舞台还给回答。
 * 用户手动开合过就记住他的选择（override），新回合思考清空时选择作废——
 * 自动规则只在用户没表态时生效，不跟用户抢。
 */
import { useEffect, useState, type ToggleEvent } from "react";

export default function ThinkingBlock({
  text,
  streaming,
  hasAnswer,
}: {
  text: string;
  streaming: boolean;
  hasAnswer: boolean;
}) {
  const [override, setOverride] = useState<boolean | null>(null);
  const autoOpen = streaming && text.length > 0 && !hasAnswer;
  const open = override ?? autoOpen;

  useEffect(() => {
    // 新回合：思考被清空 → 上一轮的手动开合作废
    if (!text) setOverride(null);
  }, [text]);

  // onToggle 分不清「用户点的」和「React 自己改 open 属性触发的」：
  // 自动展开/收起时浏览器同样会发 toggle。 naive 地把事件里的状态存进
  // override，会把第一次自动展开固化成「手动选择」，之后正文到达也
  // 不再自动收起（2026-10-24 实测踩到）。
  // 判据：事件状态与本 render 渲染出来的状态不一致，才是用户动的手。
  function handleToggle(e: ToggleEvent<HTMLDetailsElement>) {
    if (e.currentTarget.open !== open) setOverride(e.currentTarget.open);
  }

  if (!text) return null;
  return (
    <details
      className="thinking"
      open={open}
      data-testid="thinking-block"
      onToggle={handleToggle}
    >
      <summary>思考 {text.length} 字</summary>
      <div className="thinking-body">{text}</div>
    </details>
  );
}
