/**
 * 工作流画布的运行状态偏表（P2-1）。
 *
 * 运行时只改这张表、绝不增删画布的节点/边——之前直接 setNodes 改 status，
 * 会在 ReactFlow 内部引发节点重建，拖拽中的连线可能断、视口会跳。
 * 渲染层用 useMemo 把偏表叠加成 displayNodes，节点集合本身不动。
 */
export type StepRunStatus = "pending" | "running" | "done" | "failed" | "waiting";

export interface StepRunInfo {
  status: StepRunStatus;
  /** 仅 failed 有：引擎报的错误文本，挂在徽标 tooltip 上 */
  error?: string;
}

/** 节点 id → 运行状态。空表 = 本轮还没跑或已重置。 */
export type RunStatusTable = Record<string, StepRunInfo>;

/** 每轮运行开始时重置。 */
export function emptyRunStatus(): RunStatusTable {
  return {};
}

/**
 * 把偏表叠加到节点数组上（不修改原数组）：
 * - 有状态：data.status / data.runError 带上，供 DaNode 渲染徽标；
 * - 没状态但本轮已激活（表非空）：data.unreached = true，渲染层置灰而不是隐藏。
 */
export function applyRunStatus<N extends { id: string; data: Record<string, unknown> }>(
  nodes: N[],
  table: RunStatusTable,
): N[] {
  const active = Object.keys(table).length > 0;
  return nodes.map((n) => {
    const info = table[n.id];
    if (info) {
      return {
        ...n,
        data: {
          ...n.data,
          status: info.status,
          runError: info.error || "",
          unreached: false,
          runActive: active,
        },
      };
    }
    return {
      ...n,
      data: { ...n.data, status: "", runError: "", unreached: active, runActive: active },
    };
  });
}
