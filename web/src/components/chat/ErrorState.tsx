/**
 * 错误三级 + 原地重试。
 *
 *   warn   黄条：不拦路，告诉用户一声（限流/配额类，等一会就好）
 *   error  红条：这一轮没了，给「重试」按钮
 *   fatal  整块替换：这一轮彻底没救，引导重新发起
 *
 * 重试 = 重新生成最后一条（走 regenerate，不新增 user 消息——
 * 原地重试补发一条同样的 user 消息会把对话弄出重复轮次）。
 * attempt/maxAttempts 上限 3：同一个错误连点 4 次没有意义，
 * 到量把按钮收了，让用户去改输入或等限流窗口。
 */
import StateDot from "../StateDot";

export type ErrorSeverity = "warn" | "error" | "fatal";

export default function ErrorState({
  message,
  severity = "error",
  retryable = false,
  onRetry,
  attempt = 0,
  maxAttempts = 3,
  busy = false,
}: {
  message: string;
  severity?: ErrorSeverity;
  retryable?: boolean;
  onRetry?: () => void;
  attempt?: number;
  maxAttempts?: number;
  busy?: boolean;
}) {
  const exhausted = attempt >= maxAttempts;
  const showRetry = !!onRetry && retryable && !exhausted && severity !== "fatal";

  if (severity === "fatal") {
    return (
      <div className="error-state fatal" data-testid="error-state" data-severity="fatal" role="alert">
        <div className="error-state-title">这一轮无法继续</div>
        <div className="error-state-body">{message}</div>
        {onRetry && (
          <button className="btn btn-primary btn-sm" data-testid="error-restart" onClick={onRetry}>
            重新发起
          </button>
        )}
      </div>
    );
  }

  return (
    <div
      className={`error-state ${severity}`}
      data-testid="error-state"
      data-severity={severity}
      role="alert"
    >
      <span className="error-state-icon" aria-hidden>
        {severity === "warn" ? "⚠" : "✕"}
      </span>
      <span className="error-state-body">{message}</span>
      {showRetry && (
        <button
          className="btn btn-ghost btn-sm"
          data-testid="error-retry"
          disabled={busy}
          onClick={onRetry}
        >
          {busy ? <StateDot status="running" label="重试中" /> : null}
          重试{attempt > 0 ? `（${attempt}/${maxAttempts}）` : ""}
        </button>
      )}
      {exhausted && retryable && (
        <span className="tiny faint">已重试 {maxAttempts} 次，稍后再试</span>
      )}
    </div>
  );
}
