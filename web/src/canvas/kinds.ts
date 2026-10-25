/** 节点类型元数据（从 Canvas.tsx 抽出，schema 与画布共用）。 */
export type Kind = "start" | "llm" | "rag" | "tool" | "gate" | "approval" | "output" | "artifact";

export const KIND_META: Record<Kind, { label: string; color: string }> = {
  start: { label: "开始", color: "#2f9e6e" },
  llm: { label: "模型", color: "#6e56cf" },
  rag: { label: "检索", color: "#0e8a9e" },
  tool: { label: "工具", color: "#c27a10" },
  gate: { label: "逻辑门", color: "#8a63d2" },
  approval: { label: "审批", color: "#d64550" },
  output: { label: "输出", color: "#3b6fd4" },
  artifact: { label: "制品", color: "#b0416e" },
};
