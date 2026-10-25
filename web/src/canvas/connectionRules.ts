/**
 * 画布连线规则（P2-3）：纯函数，返回 null = 允许，否则返回给 toast 的原因。
 *
 * 三条规则（按序判定，命中即拦）：
 *  ① 自环：source === target；
 *  ② 成环：加上这条边后从 target 沿现有边能走回 source（DFS）；
 *  ③ 单入锚点占用：目标节点已有入边。gate 节点是逻辑汇聚点，允许多入，
 *     其余节点（工具网关语义）一条入边就够——多入让「取哪个上游」变得含糊。
 */

export interface RuleNode {
  id: string;
  kind: string;
  label?: string;
}

export interface GraphShape {
  nodes: RuleNode[];
  edges: Array<{ source: string; target: string }>;
}

/** 允许多条入边的节点类型（汇聚语义）。 */
export const MULTI_IN_KINDS = new Set(["gate"]);

function displayName(nodes: RuleNode[], id: string): string {
  const n = nodes.find((x) => x.id === id);
  return (n && (n.label || n.id)) || id;
}

export function checkConnection(
  shape: GraphShape,
  source: string,
  target: string,
): string | null {
  // ① 自环
  if (source === target) {
    return "不能连接节点自身";
  }

  const known = shape.nodes.some((n) => n.id === source) && shape.nodes.some((n) => n.id === target);
  if (!known) {
    return "连线两端的节点不存在";
  }

  // ② 成环（DFS：从 target 沿现有边出发，能否到达 source）
  const adj = new Map<string, string[]>();
  for (const e of shape.edges) {
    const list = adj.get(e.source) || [];
    list.push(e.target);
    adj.set(e.source, list);
  }
  const seen = new Set<string>([target]);
  const stack = [target];
  while (stack.length) {
    const cur = stack.pop() as string;
    if (cur === source) {
      return `会产生循环依赖：${displayName(shape.nodes, source)} → ${displayName(shape.nodes, target)} → … → ${displayName(shape.nodes, source)}`;
    }
    for (const nxt of adj.get(cur) || []) {
      if (!seen.has(nxt)) {
        seen.add(nxt);
        stack.push(nxt);
      }
    }
  }

  // ③ 单入锚点占用（gate 豁免）
  const tNode = shape.nodes.find((n) => n.id === target);
  const multiIn = tNode && MULTI_IN_KINDS.has(tNode.kind);
  if (!multiIn && shape.edges.some((e) => e.target === target)) {
    return `「${displayName(shape.nodes, target)}」只能有一条入边（逻辑门节点可多入）`;
  }

  return null;
}
