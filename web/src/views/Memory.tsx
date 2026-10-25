/**
 * 记忆页（V4.0 N7）：查看/新增/编辑/删除跨会话记忆。
 * 记忆只记用户明确表达的偏好与事实；上限 20 条、单条 1000 字（后端校验）。
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { SkeletonList } from "../components/Skeleton";
import { formatTime } from "../lib/format";
import { confirmDialog, openModal, toast } from "../store/app";

interface MemoryRow {
  id: string;
  content: string;
  kind: string;
  created_at: string;
  /** P1-9：最近一次变更类型（added/updated/existing），老行为空不显示徽标 */
  change?: string;
  [k: string]: unknown;
}

/** 变更三态徽标：新增=绿、更新=琥珀、已存在=中性（去重命中，没写新条）。 */
const CHANGE_BADGE: Record<string, { text: string; cls: string }> = {
  added: { text: "新增", cls: "badge-ok" },
  updated: { text: "更新", cls: "badge-accent" },
  existing: { text: "已存在", cls: "badge" },
};

export default function Memory() {
  const [rows, setRows] = useState<MemoryRow[]>([]);
  const [loading, setLoading] = useState(true);

  const paint = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api<{ memories: MemoryRow[] }>("/api/memory");
      setRows(d.memories || []);
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void paint();
  }, [paint]);

  function addMemory() {
    let content = "";
    openModal({
      title: "添加记忆",
      body: (
        <div className="field">
          <label htmlFor="mem-new">内容（一句话说清一件事）</label>
          <textarea
            id="mem-new"
            rows={4}
            maxLength={1000}
            placeholder="例：公文正文用仿宋三号，标题用方正小标宋。"
            onChange={(e) => (content = e.target.value)}
          />
        </div>
      ),
      actions: [
        { label: "取消" },
        {
          label: "保存",
          kind: "btn-primary",
          onClick: async (close) => {
            if (!content.trim()) return;
            try {
              await api("/api/memory", { method: "POST", body: { content: content.trim() } });
              close();
              toast("已记住", "ok");
              await paint();
            } catch (err) {
              toast((err as Error).message, "error");
            }
          },
        },
      ],
    });
  }

  function editMemory(row: MemoryRow) {
    let content = row.content;
    openModal({
      title: "编辑记忆",
      body: (
        <div className="field">
          <label htmlFor="mem-edit">内容</label>
          <textarea id="mem-edit" rows={4} maxLength={1000} defaultValue={row.content} onChange={(e) => (content = e.target.value)} />
        </div>
      ),
      actions: [
        { label: "取消" },
        {
          label: "保存",
          kind: "btn-primary",
          onClick: async (close) => {
            if (!content.trim()) return;
            try {
              await api(`/api/memory/${row.id}`, { method: "PATCH", body: { content: content.trim() } });
              close();
              toast("已更新", "ok");
              await paint();
            } catch (err) {
              toast((err as Error).message, "error");
            }
          },
        },
      ],
    });
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>记忆</h1>
          <div className="page-sub">跨会话记住的偏好与事实。写作时自动带上，不确定的不会乱记。</div>
        </div>
        <div className="row">
          <span className="tiny faint">{rows.length}/20 条</span>
          <button className="btn btn-primary btn-sm" data-testid="memory-add" onClick={addMemory}>
            添加
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => void paint()}>
            刷新
          </button>
        </div>
      </div>

      {loading ? (
        <SkeletonList rows={3} />
      ) : rows.length === 0 ? (
        <div className="empty" data-testid="memory-empty">
          <h4>还没有记忆</h4>
          <p>对话里说「记住：……」，或点右上角手动添加。</p>
        </div>
      ) : (
        <div className="memory-list" data-testid="memory-list">
          {rows.map((r) => (
            <div key={r.id} className="card memory-item" data-testid="memory-item">
              <p className="small" style={{ margin: "0 0 8px", whiteSpace: "pre-wrap" }}>
                {r.content}
              </p>
              <div className="row-between">
                <span className="tiny faint row" style={{ gap: 6 }}>
                  <span>
                    {formatTime(r.created_at)}
                    {r.kind && r.kind !== "note" ? ` · ${r.kind}` : ""}
                  </span>
                  {r.change && CHANGE_BADGE[r.change] && (
                    <span className={`badge ${CHANGE_BADGE[r.change].cls}`} data-testid="memory-change">
                      {CHANGE_BADGE[r.change].text}
                    </span>
                  )}
                </span>
                <span className="row" style={{ gap: 4 }}>
                  <button className="btn btn-ghost btn-xs" onClick={() => editMemory(r)}>
                    编辑
                  </button>
                  <button
                    className="btn btn-ghost btn-xs"
                    data-testid="memory-forget"
                    onClick={async () => {
                      if (!(await confirmDialog("忘记这条记忆？之后不会再带上它。"))) return;
                      await api(`/api/memory/${r.id}`, { method: "DELETE" });
                      toast("已忘记", "ok");
                      await paint();
                    }}
                  >
                    忘记
                  </button>
                </span>
              </div>
            </div>
          ))}
        </div>
      )}

      <p className="tiny faint" style={{ marginTop: 14 }}>
        记忆开关与评审由服务端 MEMORY_ENABLED 控制；回合后模型会自动评审新内容是否值得记。
      </p>
    </div>
  );
}
