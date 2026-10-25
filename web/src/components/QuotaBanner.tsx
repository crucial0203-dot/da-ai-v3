/**
 * 配额横幅（P1-8）：侧栏底部的今日额度条，三态——
 *
 *   normal     用量 < 90%：原样展示，不打扰；
 *   tight      ≥ 90%：琥珀色 + 「即将用尽」，给用户留出「省着点用」的反应时间；
 *   exhausted  用尽：红色 + 还剩多久重置（额度按天清，resetsIn 算到本地零点）。
 *
 * 根节点保留 data-testid="usage-bar"：验收脚本（p1_smoke 等）按它找，
 * 改这个 testid 会把一整批测试打瞎。
 */
export interface QuotaBannerProps {
  used: number;
  quota: number;
}

/** 距本地零点的毫秒数。额度按 db.today() 清，就是本地日的更替。 */
function msToLocalMidnight(): number {
  const now = new Date();
  const next = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 0, 0, 0, 0);
  return next.getTime() - now.getTime();
}

function fmtReset(ms: number): string {
  const totalMin = Math.max(1, Math.round(ms / 60000));
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h > 0 ? `${h} 小时 ${m} 分` : `${m} 分钟`;
}

export default function QuotaBanner({ used, quota }: QuotaBannerProps) {
  if (!quota || quota <= 0) return null;
  const pct = Math.min(100, Math.round((used / quota) * 100));
  const exhausted = used >= quota;
  const tight = !exhausted && pct >= 90;
  const barColor = exhausted ? "var(--danger)" : tight ? "var(--warn)" : "var(--accent)";

  return (
    <div className="side-foot" data-testid="usage-bar" data-state={exhausted ? "exhausted" : tight ? "tight" : "normal"}>
      <div className="row-between tiny">
        <span>
          今日 {used} / {quota} 次
        </span>
        {exhausted ? (
          <span style={{ color: "var(--danger)" }}>已用完</span>
        ) : (
          <span className="faint">剩 {Math.max(0, quota - used)}</span>
        )}
      </div>
      <div style={{ height: 3, background: "var(--line-soft)", borderRadius: 2, overflow: "hidden", marginTop: 5 }}>
        <div
          style={{
            height: "100%",
            width: `${pct}%`,
            background: barColor,
            transition: "width .3s",
          }}
        />
      </div>
      {tight && !exhausted && (
        <div className="tiny" style={{ color: "var(--warn)", marginTop: 4 }}>
          即将用尽，注意安排
        </div>
      )}
      {exhausted && (
        <div className="tiny" style={{ color: "var(--danger)", marginTop: 4 }}>
          今日额度已用完 · {fmtReset(msToLocalMidnight())}后重置
        </div>
      )}
    </div>
  );
}
