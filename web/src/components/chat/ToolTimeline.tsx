/**
 * 工具调用时间线。
 *
 * 两条折叠规则，都是为「一轮对话可能调十几次工具」设计的：
 *   ① visibleSteps=3：只摊开最近 3 步，更早的收进「前 N 步」的 details 里，
 *      信息不丢但不占地方——工具回路跑满 8 轮时屏幕不会被卡片淹没；
 *   ② 同类连续折叠：相邻的同类调用（如连搜三次）合成一组，组头显示
 *      「🔍 检索 ×3」，成员缩进展开。组内状态取最显眼的那个
 *      （running > waiting > error > done）——有一个还在跑，组就不能显示完成。
 */
import ToolCallCard, { type ToolCallData } from "./ToolCallCard";
import { kindIcon, kindLabel, kindOfTool, type ToolKind } from "../../lib/toolKind";

const VISIBLE_STEPS = 3;

type Step =
  | { kind: "single"; tool: ToolCallData }
  | { kind: "group"; kindName: ToolKind; tools: ToolCallData[] };

function groupSteps(tools: ToolCallData[]): Step[] {
  const steps: Step[] = [];
  for (const tool of tools) {
    const k = tool.kind ?? kindOfTool(tool.name);
    const last = steps[steps.length - 1];
    if (last && last.kind === "group" && last.kindName === k) {
      last.tools.push(tool);
      continue;
    }
    if (last && last.kind === "single" && (last.tool.kind ?? kindOfTool(last.tool.name)) === k) {
      steps[steps.length - 1] = { kind: "group", kindName: k, tools: [last.tool, tool] };
      continue;
    }
    steps.push({ kind: "single", tool });
  }
  return steps;
}

const RANK: Record<string, number> = { running: 3, waiting: 2, error: 1, done: 0 };

function groupStatus(tools: ToolCallData[]): string {
  let best = "done";
  for (const t of tools) {
    if ((RANK[t.status] ?? -1) > (RANK[best] ?? -1)) best = t.status;
  }
  return best;
}

export default function ToolTimeline({ tools }: { tools: ToolCallData[] }) {
  if (tools.length === 0) return null;
  const steps = groupSteps(tools);
  const recent = steps.slice(-VISIBLE_STEPS);
  const older = steps.slice(0, Math.max(0, steps.length - VISIBLE_STEPS));

  return (
    <div className="tool-tray" data-testid="tool-tray">
      {older.length > 0 && (
        <details className="tool-group tool-older" data-testid="tool-older">
          <summary>前 {older.length} 步已折叠</summary>
          <div className="tool-group-body">
            {older.map((step, i) => (
              <StepView key={i} step={step} />
            ))}
          </div>
        </details>
      )}
      {recent.map((step, i) => (
        <StepView key={`r${i}`} step={step} />
      ))}
    </div>
  );
}

function StepView({ step }: { step: Step }) {
  if (step.kind === "single") return <ToolCallCard tool={step.tool} />;
  const status = groupStatus(step.tools);
  const done = step.tools.filter((t) => t.status === "done").length;
  return (
    <details className="tool-group" data-testid="tool-group" data-kind={step.kindName} data-status={status}>
      <summary>
        <span className="tool-icon" aria-hidden>{kindIcon(step.kindName)}</span>
        <span className="tool-label">{kindLabel(step.kindName)}</span>
        <span className="tool-count">×{step.tools.length}</span>
        <span className="tool-status">
          {status === "done" ? `完成 ${done}/${step.tools.length}` : status === "running" ? "执行中…" : status === "waiting" ? "等待审批" : "有失败"}
        </span>
      </summary>
      <div className="tool-group-body">
        {step.tools.map((t, i) => (
          <ToolCallCard key={t.call_id || `${t.name}-${i}`} tool={t} />
        ))}
      </div>
    </details>
  );
}
