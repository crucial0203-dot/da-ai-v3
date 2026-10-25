/**
 * P1-4 停止卡：挂在被停止的半截回复下面。
 *
 * 只做展示与派发，API 调用都在 Task 层（和审批卡一个套路）——
 * 组件不自己发请求，刷新/重载的时机由页面统一拿捏。
 */
export function StoppedRun({
  kept,
  busy,
  onContinue,
  onDiscard,
}: {
  kept: number;
  busy: boolean;
  onContinue: () => void;
  onDiscard: () => void;
}) {
  return (
    <div className="stopped-run" data-testid="stopped-run">
      <div className="row-between">
        <span className="stopped-run-note">已停止 · 保留了 {kept} 字</span>
        <div className="row" style={{ gap: 6 }}>
          <button
            className="btn btn-sm"
            data-testid="continue-btn"
            disabled={busy}
            title={busy ? "正在生成中" : "从停下的地方接着写"}
            onClick={onContinue}
          >
            {busy ? "生成中…" : "继续写"}
          </button>
          <button
            className="btn btn-ghost btn-sm"
            data-testid="discard-btn"
            disabled={busy}
            title="丢弃这段半截回复（不再显示，审计里仍在）"
            onClick={onDiscard}
          >
            丢弃
          </button>
        </div>
      </div>
      <div className="tiny faint" style={{ marginTop: 2 }}>
        继续写会从停下的地方接着写完；丢弃后这段半截回复不再显示。
      </div>
    </div>
  );
}
