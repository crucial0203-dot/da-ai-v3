/**
 * 工具语义分类的镜像表。
 *
 * 后端 `app/services/tools/kinds.py` 是唯一事实源；这份同源映射只为两件事：
 *   ① 旧后端（没带 kind 字段的历史 SSE 事件）按名字兜底；
 *   ② 不依赖后端响应的地方（如静态预览）先有个分类用。
 * 新增工具时先改后端映射表，这里同步补一行——两边漂移比没有兜底更糟。
 */

export type ToolKind =
  | "read"
  | "edit"
  | "delete"
  | "move"
  | "search"
  | "execute"
  | "think"
  | "fetch"
  | "switch_mode"
  | "other";

export const TOOL_KINDS: ToolKind[] = [
  "read", "edit", "delete", "move", "search",
  "execute", "think", "fetch", "switch_mode", "other",
];

/** kind → 图标（单字形，跟着文字走，不抢注意力）。 */
export const KIND_ICONS: Record<ToolKind, string> = {
  read: "📖",
  edit: "✏️",
  delete: "🧹",
  move: "📦",
  search: "🔍",
  execute: "🧮",
  think: "🧠",
  fetch: "🌐",
  switch_mode: "🔀",
  other: "⚙️",
};

/** kind → 中文短名。 */
export const KIND_LABELS: Record<ToolKind, string> = {
  read: "读取",
  edit: "编辑",
  delete: "清理",
  move: "移动",
  search: "检索",
  execute: "计算",
  think: "思考",
  fetch: "抓取",
  switch_mode: "切换",
  other: "调用",
};

const TOOL_KIND_MAP: Record<string, ToolKind> = {
  // V3 技能工具
  "file.read": "read",
  "reference.verify": "search",
  "minimax.generate": "think",
  "pptx.create": "edit",
  "pptx.modify": "edit",
  "pptx.validate": "read",
  "artifact.write": "edit",
  // V4.0 具名工具
  web_search: "search",
  fetch_url: "fetch",
  rag_query: "search",
  memory_query: "read",
  artifact_create: "edit",
  doc_generate: "edit",
  file_list: "read",
  // 市场工具包
  calc: "execute",
  text_stats: "read",
  render_template: "edit",
  date_calc: "execute",
  unit_convert: "execute",
  json_format: "edit",
  regex_test: "execute",
  md_table: "edit",
  text_dedupe: "delete",
  cn_number: "execute",
  rand_gen: "execute",
  encode_text: "edit",
  // 合成事件
  _parse: "other",
};

/**
 * 工具名 → 中文名兜底（旧后端 / 历史消息没带 label 时用）。
 * 与后端 `registry.py` 的 ToolSpec.label、市场 `catalog.json` 的 label 对齐；
 * 认不出的名字原样显示——比笼统的「调用」更能让用户知道发生了什么。
 */
const FALLBACK_LABELS: Record<string, string> = {
  _parse: "工具调用解析",
  // V4.0 具名工具
  web_search: "联网检索",
  fetch_url: "抓取网页",
  rag_query: "知识库检索",
  memory_query: "读记忆",
  artifact_create: "创建制品",
  doc_generate: "生成文档",
  file_list: "列出文件",
  // 市场工具包
  calc: "四则运算",
  text_stats: "文本统计",
  render_template: "模板渲染",
  date_calc: "日期计算",
  unit_convert: "单位换算",
  json_format: "JSON 处理",
  regex_test: "正则测试",
  md_table: "Markdown 表格",
  text_dedupe: "文本去重",
  cn_number: "中文数字转换",
  rand_gen: "随机生成",
  encode_text: "编码与摘要",
  // V3 技能工具
  "file.read": "读取文件",
  "reference.verify": "出处核验",
  "minimax.generate": "模型生成",
  "pptx.create": "创建 PPTX",
  "pptx.modify": "修改 PPTX",
  "pptx.validate": "校验 PPTX",
  "artifact.write": "写入交付物",
};

export function kindOfTool(name: string): ToolKind {
  return TOOL_KIND_MAP[name] ?? "other";
}

/** 事件没带 label 时兜底：先查内置表，再退回名字本身。 */
export function labelOfTool(name: string, backendLabel?: string): string {
  if (backendLabel) return backendLabel;
  return FALLBACK_LABELS[name] ?? name;
}

export function kindIcon(kind: ToolKind): string {
  return KIND_ICONS[kind] ?? KIND_ICONS.other;
}

export function kindLabel(kind: ToolKind): string {
  return KIND_LABELS[kind] ?? KIND_LABELS.other;
}
