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

interface ArtifactTurnCardProps {
  task?: { id?: string; updated_at?: string } | null;
  /** 宿主透传的消息流；用它算「这一回合有没有新增内容」的刷新戳 */
  messages?: Array<{ id?: string | number }>;
}

function ArtifactTurnCard({ task, messages }: ArtifactTurnCardProps) {
  const taskId = String(task?.id || "");
  // taskId 在会话里不变：只盯它的话，本轮新建的制品要等重新进页面才出现。
  // 用「消息条数 + 末条 id + updated_at」当刷新戳，每轮结束都会变。
  const lastId = messages?.length ? String(messages[messages.length - 1]?.id ?? "") : "";
  const stamp = `${task?.updated_at || ""}|${messages?.length || 0}|${lastId}`;
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
    // stamp 只用来触发重取，不参与请求
  }, [taskId, stamp]);

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
