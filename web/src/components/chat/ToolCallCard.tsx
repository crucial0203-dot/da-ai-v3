/**
 * 单次工具调用卡。
 *
 * 一张卡 = 一次调用（call_id），跨 running/waiting/done/error 四个状态合并。
 * kind/icon/label 决定长相，status 决定状态行。输入参数默认收起——
 * 大多数调用的参数用户并不关心，摊开只会把回答挤下去；出错时例外，
 * 参数往往是复现问题的关键，所以 error/waiting 默认展开。
 */
import StateDot from "../StateDot";
import { kindIcon, kindLabel, kindOfTool, labelOfTool, type ToolKind } from "../../lib/toolKind";

export interface ToolCallData {
  name: string;
  status: string;
  input?: Record<string, unknown>;
  error?: string;
  call_id?: string;
  kind?: ToolKind;
  label?: string;
  ts?: number;
  seq?: number;
  /** 流式围栏阶段（S2-3）：参数还在逐字写，input 是 fix-json 补全出的部分字段 */
  streaming_args?: boolean;
}

const STATUS_TEXT: Record<string, string> = {
  running: "执行中",
  waiting: "等待审批",
  done: "完成",
  error: "失败",
};

function fmtInput(input: Record<string, unknown> | undefined): string {
  if (!input || Object.keys(input).length === 0) return "";
  try {
    return JSON.stringify(input, null, 2);
  } catch {
    return String(input);
  }
}

export default function ToolCallCard({ tool }: { tool: ToolCallData }) {
  const kind = tool.kind ?? kindOfTool(tool.name);
  const label = labelOfTool(tool.name, tool.label);
  const inputText = fmtInput(tool.input);
  const expand = tool.status === "error" || tool.status === "waiting" || tool.status === "running";
  // S2-3：围栏还在写（streaming_args）且没结束时，参数是部分字段——
  // 与 assistant-ui 的 ToolCallMessagePart.args 同语义：流式期间是半截的。
  const argsWriting = Boolean(tool.streaming_args)
    && tool.status !== "done" && tool.status !== "error";
  const dot =
    tool.status === "running" ? "running"
    : tool.status === "waiting" ? "idle"
    : tool.status === "done" ? "done"
    : tool.status === "error" ? "failed"
    : "idle";

  return (
    <div
      className={`tool-card kind-${kind} ${tool.status}${argsWriting ? " args-writing" : ""}`}
      data-testid="tool-card"
      data-kind={kind}
      data-status={tool.status}
      data-call-id={tool.call_id || ""}
      data-args-writing={argsWriting ? "true" : undefined}
    >
      <div className="tool-card-head">
        <span className="tool-icon" aria-hidden>{kindIcon(kind)}</span>
        <span className="tool-label">{label}</span>
        <span className="tool-kind-chip">{kindLabel(kind)}</span>
        <span className="tool-name" title={tool.name}>{tool.name}</span>
        <span className="tool-status">
          {argsWriting && <span className="tool-writing" data-testid="tool-args-writing">参数写入中</span>}
          {dot !== "idle" && <StateDot status={dot} label={STATUS_TEXT[tool.status] || tool.status} />}
          <span className="tool-status-text">
            {tool.status === "error" ? `失败：${tool.error || "未知错误"}` : STATUS_TEXT[tool.status] || tool.status}
          </span>
        </span>
      </div>
      {inputText && (
        <details className="tool-detail" open={expand}>
          <summary>参数{argsWriting ? "（写入中…）" : ""}</summary>
          <pre className="tool-input">{inputText}</pre>
        </details>
      )}
    </div>
  );
}
