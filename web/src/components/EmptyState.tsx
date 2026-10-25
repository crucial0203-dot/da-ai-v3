/**
 * 画布空状态（P2-5）二段式：
 * - never：用户从未建过工作流 → 引导新建第一个；
 * - empty-selection：有工作流但画布上还没载入 → 引导选择或新建。
 * 空状态里不放输入框——只有说明文字与明确的动作按钮。
 */
export type EmptyPhase = "never" | "empty-selection";

export default function EmptyState({
  phase,
  count,
  onOpenList,
}: {
  phase: EmptyPhase;
  /** 工作流总数（never 判定的旁证，测试也用它区分两段） */
  count: number;
  onOpenList: () => void;
}) {
  const never = phase === "never";
  return (
    <div className="wf-empty" data-testid="wf-empty" data-phase={phase}>
      <div className="wf-empty-card">
        <h4>{never ? "还没有工作流" : "画布是空的"}</h4>
        {never ? (
          <p>
            从左侧加一个「开始」节点，接上模型与输出，就是第一个工作流。
            也可以从内置模板起步。
          </p>
        ) : (
          <p>左侧列表里有 {count} 个工作流，点一个载入画布，或直接新建。</p>
        )}
        {never ? (
          <button className="btn btn-primary btn-sm" data-testid="wf-empty-new" onClick={onOpenList}>
            新建工作流
          </button>
        ) : (
          <button className="btn btn-primary btn-sm" data-testid="wf-empty-open" onClick={onOpenList}>
            去左侧列表选择
          </button>
        )}
      </div>
    </div>
  );
}
