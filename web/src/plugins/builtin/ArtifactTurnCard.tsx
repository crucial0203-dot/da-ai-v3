/**
 * 内置插件「对话内制品卡」—— 占用 chat.turn.tail 槽位（P2-8）。
 * 对话回合结束后，把该任务的最新制品以 ArtifactCard 呈现在对话流末尾。
 * 停用即整卡消失（槽位 disposer 回滚），全部逻辑不碰宿主源码。
 */
import { useEffect, useState, type ComponentType } from "react";
import { api } from "../../lib/api";
import ArtifactCard from "../../components/chat/ArtifactCard";
import type { UIPlugin } from "../registry";

interface ArtifactRow {
  id: string;
  title: string;
  version: number;
  chars?: number;
  created_at: string;
}

function ArtifactTurnCard({ task }: { task?: { id?: string } | null }) {
  const taskId = String(task?.id || "");
  const [rows, setRows] = useState<ArtifactRow[]>([]);
  useEffect(() => {
    if (!taskId) return;
    let alive = true;
    api<{ artifacts: ArtifactRow[] }>(`/api/artifacts?task_id=${encodeURIComponent(taskId)}`)
      .then((d) => {
        if (alive) setRows(d.artifacts || []);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [taskId]);

  if (!taskId || rows.length === 0) return null;
  // 只亮最新一张（列表按 created_at DESC），其余在制品库看
  const latest = rows[0];
  return (
    <div style={{ marginTop: 10 }} data-testid="artifact-turn-card">
      <ArtifactCard
        state="done"
        title={latest.title}
        artifactId={latest.id}
        version={latest.version}
        words={latest.chars}
      />
    </div>
  );
}

export const artifactTurnCard: UIPlugin = {
  manifest: {
    id: "builtin.artifact-turn-card",
    name: "对话内制品卡",
    version: "0.1.0",
    description: "对话回合结束后在流末尾展示该任务最新制品（预览/下载）",
    ui: { slots: ["chat.turn.tail"] },
  },
  activate(ctx) {
    ctx.slots.register("chat.turn.tail", {
      id: "artifact-turn-card",
      component: ArtifactTurnCard as ComponentType<Record<string, unknown>>,
      order: 20,
    });
  },
};
