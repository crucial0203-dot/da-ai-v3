/**
 * 审批卡：模型要动真格之前，把人拦下来问一句。
 *
 * 三段式版式（方案定稿）：
 *   标题 = 动作   —— 「生成文档」这种动作短语，不是参数名堆砌
 *   警告 = 风险   —— reason 单独一条警示带，写清会出什么事
 *   影响面        —— input 逐字段一行（label: value），点之前看清会动什么
 * 按钮顺序 **Deny(幽灵) → Allow once(实心，右对齐)**。
 *
 * 主动裁剪：**不做「Always allow」**。全局放行会绕过 requires_approval
 * 安全红线，且要新增用户级放行表——语义太重，不做。
 *
 * state：request(待确认) → running(批准后执行中) → done / denied。
 * destructive（红底）只给写操作类审批：读/检索类只是看东西，不配用红色吓人。
 */
import StateDot from "../StateDot";
import { kindIcon, kindLabel, kindOfTool, type ToolKind } from "../../lib/toolKind";

export type ApprovalState = "request" | "running" | "done" | "denied";

export interface ApprovalDetail {
  label: string;
  value: string;
}

const STATE_CHIP: Record<ApprovalState, string> = {
  request: "待确认",
  running: "执行中",
  done: "已允许",
  denied: "已拒绝",
};

export default function ApprovalCard({
  toolName,
  label,
  kind,
  risk = "normal",
  reason,
  details,
  state,
  busy = false,
  onAllow,
  onDeny,
}: {
  toolName: string;
  label?: string;
  kind?: ToolKind;
  risk?: "high" | "normal";
  reason?: string;
  details: ApprovalDetail[];
  state: ApprovalState;
  busy?: boolean;
  onAllow: () => void;
  onDeny: () => void;
}) {
  const destructive = risk === "high";
  const k = kind ?? kindOfTool(toolName);
  const title = label || toolName;
  const terminal = state === "done" || state === "denied";

  return (
    <article
      className={`msg msg-system approval-card ${destructive ? "destructive" : ""} ${state}`}
      data-testid="tool-approval-card"
      data-state={state}
      data-risk={risk}
    >
      <div className="msg-label">
        工具调用需确认
        <span className="approval-state-chip">{STATE_CHIP[state]}</span>
      </div>
      <div className="approval-title">
        <span className="tool-icon" aria-hidden>{kindIcon(k)}</span>
        {title}
        <span className="tool-name" title={toolName}>{toolName}</span>
        <span className="tool-kind-chip">{kindLabel(k)}</span>
      </div>
      {reason && (
        <div className="approval-warn" data-testid="approval-reason">
          <span aria-hidden>⚠</span> {reason}
        </div>
      )}
      {details.length > 0 && (
        <dl className="approval-details" data-testid="approval-details">
          {details.map((d) => (
            <div className="approval-detail-row" key={d.label}>
              <dt>{d.label}</dt>
              <dd>{d.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {state === "request" && (
        <div className="approval-actions">
          <button
            className="btn btn-ghost btn-sm"
            data-testid="tool-reject-btn"
            disabled={busy}
            onClick={onDeny}
          >
            拒绝
          </button>
          <button
            className="btn btn-primary btn-sm"
            data-testid="tool-approve-btn"
            disabled={busy}
            onClick={onAllow}
          >
            允许执行这一次
          </button>
        </div>
      )}
      {state === "running" && (
        <div className="approval-actions">
          <span className="approval-running">
            <StateDot status="running" label="执行中" />
            正在执行，稍候续答…
          </span>
        </div>
      )}
      {state === "done" && <div className="approval-terminal ok">✓ 已允许并执行完毕</div>}
      {state === "denied" && <div className="approval-terminal muted">已拒绝，模型将自行降级处理</div>}
      {!terminal && (
        <div className="tiny faint" style={{ marginTop: 6 }}>
          批准后立即执行并续答本次对话，不额外扣次数。
        </div>
      )}
    </article>
  );
}
