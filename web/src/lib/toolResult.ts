/**
 * 工具回注块解析。
 *
 * 后端把工具结果包成
 *   <<<TOOL_RESULT name="web_search" id="call_1">>>\n{"text": "..."}\n<<<END_TOOL_RESULT>>>
 * 以 **user 消息**回注进对话——这是后端与模型之间的线上格式（模型靠它拿结果），
 * 不是用户的言论。库里已经存了 340 条这样的消息，前端一直原样渲染，用户直接在
 * 对话里看到一坨原始 JSON（含 `<<<TOOL_RESULT` 这种内部标记）。
 *
 * 本模块只做切分与摘要提取（纯函数，单测见 tests/p17_tool_result_card.js）；
 * 渲染在 Message.tsx 的 ToolResultCard。
 *
 * 容错点：块可能不闭合（流被截断，body 吃到内容结尾），header 可能缺 id——
 * 两种情况都不抛，truncated 标记交给 UI 显示「截断」徽标。
 */

export interface ToolResultBlock {
  kind: "tool";
  name: string;
  /** header 与闭合标记之间的原文（可能不是合法 JSON） */
  body: string;
  /** 没找到闭合标记——流在工具回注中间被截断 */
  truncated: boolean;
  /** 一行摘要（优先 text/error，退回键值对或原文前缀） */
  summary: string;
  /** body 里解析出 error 字段——UI 红着显示 */
  isError: boolean;
}

export interface TextSegment {
  kind: "text";
  text: string;
}

export type Segment = TextSegment | ToolResultBlock;

const HEADER = /<<<TOOL_RESULT\s+name="([^"]*)"(?:\s+id="([^"]*)")?\s*>>>/;
const CLOSE = "<<<END_TOOL_RESULT>>>";

function clip(text: string, max: number): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length <= max ? flat : flat.slice(0, max) + "…";
}

function valueText(v: unknown): string {
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v) ?? "";
  } catch {
    return "";
  }
}

/** 从回注 body 提一行摘要。解析失败（截断的 JSON）就退回原文前缀，不抛。 */
export function summarizeToolResult(body: string): { summary: string; isError: boolean } {
  const raw = body.trim();
  if (!raw) return { summary: "（空结果）", isError: false };
  let parsed: unknown = null;
  try {
    parsed = JSON.parse(raw);
  } catch {
    parsed = null;
  }
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const obj = parsed as Record<string, unknown>;
    const err = obj.error;
    if (typeof err === "string" && err.trim()) {
      return { summary: clip(err.trim(), 120), isError: true };
    }
    for (const key of ["text", "name", "title", "message"]) {
      const v = obj[key];
      if (typeof v === "string" && v.trim()) {
        return { summary: clip(v.trim(), 120), isError: false };
      }
    }
    const pairs = Object.entries(obj)
      .slice(0, 3)
      .map(([k, v]) => `${k}=${clip(valueText(v), 40)}`)
      .filter(Boolean);
    if (pairs.length) return { summary: clip(pairs.join("，"), 120), isError: false };
  }
  return { summary: clip(raw, 120), isError: false };
}

/** 把消息内容切成「文本 / 工具结果块」段。没有回注块时返回单段文本。 */
export function splitToolResults(content: string): Segment[] {
  const text = content || "";
  if (!text.includes("<<<TOOL_RESULT")) return text ? [{ kind: "text", text }] : [];
  const segs: Segment[] = [];
  let cursor = 0;
  for (;;) {
    const head = HEADER.exec(text.slice(cursor));
    if (!head) break;
    const start = cursor + head.index;
    if (start > cursor) {
      const before = text.slice(cursor, start);
      if (before) segs.push({ kind: "text", text: before });
    }
    const bodyStart = start + head[0].length;
    const closeAt = text.indexOf(CLOSE, bodyStart);
    const truncated = closeAt === -1;
    // 去首尾空行：闭合标记前必有一个换行，卡片里不该留白行
    const body = text.slice(bodyStart, truncated ? undefined : closeAt).replace(/^\n+/, "").replace(/\s+$/, "");
    const { summary, isError } = summarizeToolResult(body);
    segs.push({ kind: "tool", name: head[1] || "tool", body, truncated, summary, isError });
    if (truncated) return segs;
    cursor = closeAt + CLOSE.length;
  }
  const rest = text.slice(cursor);
  if (rest) segs.push({ kind: "text", text: rest });
  return segs;
}
