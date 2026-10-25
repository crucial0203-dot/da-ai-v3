/**
 * 画布自动布局（S2-7）：dagre 分层整理。
 *
 * 移植来源：xyflow 官方 dagre 示例（`@dagrejs/dagre` 是 dagre 的维护分支）。
 * 用途：节点越加越乱之后一键还原成从上到下的分层图。
 *
 * 三条设计约束：
 *  ① 只改 position，不动 id/type/data —— 布局≠逻辑，与 resume 指纹
 *     （engine.graph_fingerprint 不哈希 position）口径一致，整理完布局
 *     不会让暂停的运行记录变成 stale；
 *  ② 方向 TB：与画布既有的「start 在上、output 在下」流向一致；
 *  ③ 返回新数组（不可变），调用方直接 setNodes —— useUndo 会把它
 *     记成一步，整理布局可以撤销。
 */
import dagre from "@dagrejs/dagre";

/** 节点估算尺寸（px）。只影响间距，不影响正确性；实际渲染宽度由 CSS 决定。 */
const NODE_W = 220;
const NODE_H = 84;
/** 同层节点水平间距 / 层与层垂直间距。 */
const NODESEP = 60;
const RANKSEP = 90;

export interface LayoutNode {
  id: string;
  position: { x: number; y: number };
}

/**
 * 计算整理后的位置。edges 里出现图中不存在的节点时忽略该边
 * （dagre 对未知节点会抛错，而画布上的脏数据不该让按钮崩掉）。
 */
export function computeAutoLayout<T extends LayoutNode>(
  nodes: T[],
  edges: Array<{ source: string; target: string }>,
): T[] {
  if (nodes.length === 0) return nodes;

  const known = new Set(nodes.map((n) => n.id));
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "TB", nodesep: NODESEP, ranksep: RANKSEP });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodes) {
    g.setNode(n.id, { width: NODE_W, height: NODE_H });
  }
  for (const e of edges) {
    if (known.has(e.source) && known.has(e.target)) {
      g.setEdge(e.source, e.target);
    }
  }
  dagre.layout(g);

  return nodes.map((n) => {
    const laid = g.node(n.id) as { x: number; y: number } | undefined;
    if (!laid) return n;
    // dagre 给的是中心点，转回 xyflow 的左上角
    return { ...n, position: { x: laid.x - NODE_W / 2, y: laid.y - NODE_H / 2 } };
  });
}
