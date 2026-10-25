/**
 * 属性面板的字段 schema（P2-4）：每种节点类型的表单由这份数据驱动，
 * PropPanel 只做一个分派器，6 种控件（string/number/boolean/options/json/array）。
 *
 * SCHEMA 是纯静态的：options 候选通过 source 字符串引用（models/efforts/tools），
 * 渲染时才解析——这样 cleanData / buildGraph 也能直接用这份数据清洗载荷，
 * 不用把 meta 传得到处都是。
 */
import type { Kind } from "./kinds";

export type FieldType = "string" | "number" | "boolean" | "options" | "json" | "array";

export interface FieldSpec {
  key: string;
  label: string;
  type: FieldType;
  rows?: number;
  placeholder?: string;
  /** options 类型的候选择来源 */
  source?: "models" | "efforts" | "tools" | "gate-ops";
  /** 条件显示；返回 false 时不渲染，且该字段从提交载荷剔除 */
  display?: (d: Record<string, unknown>) => boolean;
}

const GATE_OPS = ["AND", "OR", "NOT", "XOR", "NAND", "NOR", "IF"];

export const SCHEMA: Record<Kind, FieldSpec[]> = {
  start: [{ key: "label", label: "显示名", type: "string" }],
  llm: [
    { key: "label", label: "显示名", type: "string" },
    { key: "prompt", label: "提示词（必填，可用 {{变量}}）", type: "string", rows: 5, placeholder: "写一段…引用上游：{{node_id}} 或 {{last}}" },
    { key: "system", label: "系统提示（可选）", type: "string", rows: 2 },
    { key: "model", label: "模型（留空用默认）", type: "options", source: "models" },
    { key: "effort", label: "思考深度", type: "options", source: "efforts" },
  ],
  rag: [
    { key: "label", label: "显示名", type: "string" },
    { key: "query", label: "检索词（可用 {{last}}）", type: "string", placeholder: "{{last}}" },
    { key: "top_k", label: "取前 N 条", type: "number" },
  ],
  tool: [
    { key: "label", label: "显示名", type: "string" },
    { key: "tool", label: "工具", type: "options", source: "tools" },
    { key: "input", label: "入参 JSON（可用 {{变量}}）", type: "json", rows: 4, placeholder: '{"query": "{{last}}"}' },
  ],
  gate: [
    { key: "label", label: "显示名", type: "string" },
    { key: "op", label: "逻辑门", type: "options", source: "gate-ops" },
  ],
  approval: [
    { key: "label", label: "显示名", type: "string" },
    { key: "reason", label: "审批理由（必填）", type: "string", rows: 2, placeholder: "为什么这一步需要人确认" },
  ],
  output: [
    { key: "label", label: "显示名", type: "string" },
    { key: "text", label: "输出模板（{{变量}} 会被上游结果替换）", type: "string", rows: 4 },
  ],
  artifact: [
    { key: "label", label: "显示名", type: "string" },
    { key: "title", label: "制品标题", type: "string" },
    { key: "html", label: "HTML 内容", type: "string", rows: 6, placeholder: "<h1>…</h1>" },
  ],
};

/** 该 kind 生效的字段集合（display false 的剔除）——buildGraph 清洗载荷用。 */
export function fieldKeysFor(kind: Kind, data: Record<string, unknown>): Set<string> {
  const keys = new Set<string>();
  for (const f of SCHEMA[kind] || []) {
    if (!f.display || f.display(data)) keys.add(f.key);
  }
  return keys;
}

/** 按 schema 清洗 data：剔除非本 kind 的字段（display false 或未知键）。 */
export function cleanData(kind: Kind, data: Record<string, unknown>): Record<string, unknown> {
  const allowed = fieldKeysFor(kind, data);
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(data)) {
    if (allowed.has(k)) out[k] = v;
  }
  return out;
}

export { GATE_OPS };
