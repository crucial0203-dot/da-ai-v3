/** 状态点：spinner 的相位同步版。
 *
 * 为什么不用裸 .spinner：多个 spinner 同时转但**起始相位不同**，看着就像
 * 几块表各走各的，很显廉价。getAnimations() 把 startTime 归零即可同相。
 * 兜底：元素已卸载/无动画 API 时静默跳过，不影响渲染。
 */
import { useEffect, useRef } from "react";

export type DotStatus = "idle" | "running" | "done" | "failed";

const LABELS: Record<DotStatus, string> = {
  idle: "等待中",
  running: "进行中",
  done: "已完成",
  failed: "失败",
};

export default function StateDot({ status, label }: { status: DotStatus; label?: string }) {
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || status !== "running") return;
    // 同相位：所有运行中的 spinner 对齐到同一起点
    el.getAnimations?.().forEach((a) => {
      a.startTime = 0;
    });
  }, [status]);

  return (
    <span
      ref={ref}
      className={`state-dot state-dot--${status}`}
      data-status={status}
      role="status"
      aria-label={label || LABELS[status]}
      title={label || LABELS[status]}
    />
  );
}
