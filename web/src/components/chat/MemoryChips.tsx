/**
 * 记忆 chips（P1-9）：回合结束时，把这一轮真正落库的记忆操作渲染成一行小徽标。
 *
 * 数据来源是 done 事件的 memory_ops——**正文里内联** ```memory 块的同步
 * 执行结果。回合后的异步模型评审也写记忆，但那时响应已经结束，结果只落
 * memories.change 列（记忆页的徽标），不进这里的 chips。
 *
 * 三态徽标：新增（added）/ 更新（updated）/ 已存在（existing，去重命中，
 * 模型想记的这件事已经有了）。删除与失败也计数，不留 silent drop。
 */
export interface MemoryOp {
  op: string;
  id?: string;
  change?: string;
  error?: string;
}

const CHANGE_LABEL: Record<string, { text: string; cls: string }> = {
  added: { text: "新增", cls: "badge-ok" },
  updated: { text: "更新", cls: "badge-accent" },
  existing: { text: "已存在", cls: "badge" },
  deleted: { text: "删除", cls: "badge" },
};

export default function MemoryChips({ ops }: { ops: MemoryOp[] }) {
  if (!ops.length) return null;
  const tally = new Map<string, number>();
  const errors: string[] = [];
  for (const op of ops) {
    if (op.error) {
      errors.push(op.error);
      continue;
    }
    const key = op.change || op.op;
    tally.set(key, (tally.get(key) || 0) + 1);
  }
  if (!tally.size && !errors.length) return null;

  return (
    <div className="memory-chips" data-testid="memory-chips">
      <span className="tiny faint">记忆</span>
      {[...tally.entries()].map(([change, n]) => {
        const meta = CHANGE_LABEL[change] || { text: change, cls: "badge" };
        return (
          <span key={change} className={`badge ${meta.cls}`}>
            {meta.text} ×{n}
          </span>
        );
      })}
      {errors.length > 0 && (
        <span className="badge badge-warn" title={errors.join("；")}>
          {errors.length} 条未记入
        </span>
      )}
    </div>
  );
}
