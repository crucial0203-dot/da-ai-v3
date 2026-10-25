/**
 * 上下文压力条（P1-8）：这一轮喂给模型的上下文由什么构成、有多满。
 *
 * 六段：材料 / 技能 / RAG / 联网 / 记忆 / 历史。每段的 token 数是**粗估**
 * （按条数 × 单条经验值），用来看相对构成与量级，不是计费依据——
 * 模型注册表里没有各家的上下文窗口，预算取保守值 BUDGET_TOKENS，
 * 宁可早报警，不要等真被截断了才提示。
 *
 * pressure > 0.85 整条转琥珀（TIGHT），并给一句「长历史可能被截断」。
 */
export interface ContextCounts {
  materials: number;
  skills: number;
  rag: number;
  web: number;
  memory: number;
  history: number;
}

/** 保守预算：不依赖具体模型窗口（注册表未记录），超出即视为吃紧。 */
const BUDGET_TOKENS = 100_000;
const TIGHT_AT = 0.85;

/** 单条经验 token 估值的出处写在这里，改估值先改注释。 */
const SEGMENTS: Array<{ key: keyof ContextCounts; label: string; per: number; color: string }> = [
  { key: "materials", label: "材料", per: 2000, color: "var(--primary)" },
  { key: "skills", label: "技能", per: 400, color: "var(--accent)" },
  { key: "rag", label: "RAG", per: 800, color: "var(--ok)" },
  { key: "web", label: "联网", per: 600, color: "var(--warn)" },
  { key: "memory", label: "记忆", per: 120, color: "var(--danger)" },
  { key: "history", label: "历史", per: 400, color: "var(--text-2)" },
];

/** 从 SSE meta 事件取六段计数。旧后端没有 memory_count/history 两个字段，
 *  缺失按 0——压力条少两段而已，不算错误。 */
export function ctxFromMeta(data: Record<string, unknown>): ContextCounts {
  return {
    materials: Number(data.materials || 0),
    skills: Array.isArray(data.skills) ? data.skills.length : 0,
    rag: Number(data.rag || 0),
    web: Number(data.web || 0),
    memory: Number(data.memory_count || 0),
    history: Number(data.history || 0),
  };
}

export default function ContextPressure({ counts }: { counts: ContextCounts }) {
  const used = SEGMENTS.reduce((sum, s) => sum + counts[s.key] * s.per, 0);
  const pressure = used / BUDGET_TOKENS;
  const tight = pressure > TIGHT_AT;
  const headroom = Math.max(0, BUDGET_TOKENS - used);
  const present = SEGMENTS.filter((s) => counts[s.key] > 0);

  return (
    <div
      className="ctx-pressure"
      data-testid="context-pressure"
      data-tight={tight ? "1" : "0"}
      title={`估算占用约 ${(used / 1000).toFixed(1)}k / ${BUDGET_TOKENS / 1000}k tokens`}
    >
      <div className="row-between tiny">
        <span style={tight ? { color: "var(--warn)" } : undefined}>上下文压力</span>
        <span className="faint mono">余量 ~{headroom >= 1000 ? `${(headroom / 1000).toFixed(1)}k` : headroom}</span>
      </div>
      {present.length === 0 ? (
        <div className="tiny faint" style={{ marginTop: 4 }}>本轮上下文是空的</div>
      ) : (
        <>
          <div className="ctx-bar">
            {present.map((s) => (
              <i
                key={s.key}
                style={{
                  width: `${Math.min(100, (counts[s.key] * s.per * 100) / Math.max(used, 1))}%`,
                  background: s.color,
                }}
                title={`${s.label} ×${counts[s.key]}`}
              />
            ))}
          </div>
          <div className="ctx-legend">
            {present.map((s) => (
              <span key={s.key} className="tiny faint">
                <i className="ctx-dot" style={{ background: s.color }} />
                {s.label} ×{counts[s.key]}
              </span>
            ))}
          </div>
          {tight && (
            <div className="tiny" style={{ color: "var(--warn)", marginTop: 2 }}>
              上下文偏满，更早的历史可能被截断
            </div>
          )}
        </>
      )}
    </div>
  );
}
