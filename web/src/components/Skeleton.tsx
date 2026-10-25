/** 骨架屏：首屏加载占位。复用 base.css 的 .skeleton（shimmer 动画）。
 *
 * 三种密度对应三类页面形态：
 *   SkeletonBlock  一行/一块（详情页、设置项）
 *   SkeletonList   N 行块（列表页：插件/技能/记忆/知识库）
 *   SkeletonCard   N 张卡（网格页：市场发现页、管理台统计卡）
 */
import type { CSSProperties } from "react";

export function SkeletonBlock({ width = "100%", height }: { width?: string | number; height?: number }) {
  const style: CSSProperties = { width };
  if (height) style.height = height;
  return <div className="skeleton" style={style} aria-hidden="true" />;
}

export function SkeletonList({ rows = 4 }: { rows?: number }) {
  // 末两行递减宽度，避免「整齐划一」的假感
  const widths = ["100%", "88%", "72%", "64%", "80%", "56%"];
  return (
    <div aria-hidden="true">
      {Array.from({ length: rows }, (_, i) => (
        <SkeletonBlock key={i} width={widths[i % widths.length]} />
      ))}
    </div>
  );
}

export function SkeletonCard({ cards = 3 }: { cards?: number }) {
  return (
    <div className="sk-cards" aria-hidden="true">
      {Array.from({ length: cards }, (_, i) => (
        <div className="sk-card" key={i}>
          <SkeletonBlock width="42%" height={18} />
          <SkeletonBlock width="100%" />
          <SkeletonBlock width="76%" />
        </div>
      ))}
    </div>
  );
}

/** 微光文字标签：生成进行时用（如「正在写」）。
 *  复用 .skeleton 的 shimmer 动画，但把渐变裁到文字上（background-clip: text）。 */
export function ShimmerLabel({ text }: { text: string }) {
  return <span className="shimmer-label">{text}</span>;
}
