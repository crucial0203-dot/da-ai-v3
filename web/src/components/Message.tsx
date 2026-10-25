/** 消息渲染：用户/助手/交付卡片，thinking 折叠、markdown、复制与重生成。 */
import { useMemo, useState } from "react";
import { formatSize, formatTime } from "../lib/format";
import { renderMarkdown } from "../lib/md";
import { splitToolResults, type ToolResultBlock } from "../lib/toolResult";
import { kindIcon, kindLabel, kindOfTool } from "../lib/toolKind";
import { confirmDialog, openModal, toast } from "../store/app";
import { api } from "../lib/api";
import { StoppedRun } from "./StoppedRun";

export interface Deliverable {
  type: string;
  file_id: string;
  name: string;
  size?: number;
  report?: { ok?: boolean; passed?: number; total?: number; checks?: Array<{ name: string; ok: boolean; detail?: string }> };
}

export interface Msg {
  id: number;
  role: string;
  content: string;
  thinking: string;
  tokens_in: number;
  tokens_out: number;
  cache_read: number;
  status: string;
  created_at: string;
  plan_flag?: boolean;
  skills?: string;
  web_flag?: boolean;
  web_sources?: Array<{ n: number; title: string; url: string; provider?: string }>;
  // P1-4/P1-5：老后端不带这三个字段，缺省按「未停止、无分支」处理
  stopped?: boolean;
  parent_id?: number | null;
  branch_index?: number | null;
  [k: string]: unknown;
}

export function DeliverableCard({ d }: { d: Deliverable }) {
  const report = d.report || {};
  const checks = report.checks || [];
  return (
    <div className="deliverable" data-testid="deliverable-card">
      <div className="row-between">
        <span className="small grow truncate" title={d.name}>
          {d.name}
        </span>
        <a className="btn btn-sm" href={`/api/files/${d.file_id}`} download>
          下载
        </a>
      </div>
      <div className="tiny faint" style={{ marginTop: 2 }}>
        {formatSize(d.size || 0)}
      </div>
      {checks.length > 0 && (
        <div className="checks">
          <span className={`badge ${report.ok ? "badge-ok" : "badge-warn"}`}>
            校验 {report.passed || 0}/{report.total || 0}
          </span>
          {checks.map((c) => (
            <div key={c.name} className={`check-row ${c.ok ? "check-ok" : "check-no"}`}>
              <span>{c.name}</span>
              <span className="faint">{c.detail || ""}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function CodeCopy({ html }: { html: string }) {
  // markdown 渲染出的 <pre> 由 dangerouslySetInnerHTML 注入；
  // 复制按钮改为流结束后由 Task 层的 effect 补挂，这里只负责渲染。
  return <div className="md" dangerouslySetInnerHTML={{ __html: html }} />;
}

/** 工具回注卡：后端把工具结果以 user 消息回注给模型（<<<TOOL_RESULT>>> 块），
 * 那是线上格式不是用户言论——渲染成一行摘要，默认折叠，展开看原始 JSON。 */
function ToolResultCard({ block }: { block: ToolResultBlock }) {
  const kind = kindOfTool(block.name);
  return (
    <details
      className={`tool-result-card${block.isError ? " error" : ""}`}
      data-testid="tool-result-card"
      data-tool={block.name}
      data-truncated={block.truncated || undefined}
    >
      <summary>
        <span aria-hidden>{kindIcon(kind)}</span>
        <span className="tool-result-name">{kindLabel(kind)}</span>
        <span className="grow truncate tool-result-summary">{block.summary}</span>
        {block.truncated && <span className="badge badge-warn">截断</span>}
      </summary>
      <pre className="tool-result-body">{block.body}</pre>
    </details>
  );
}

export function Message({
  m,
  isLastAi,
  onRegenerate,
  siblings,
  onSwitch,
  onContinue,
  onDiscard,
  busy,
}: {
  m: Msg;
  isLastAi?: boolean;
  onRegenerate?: () => void;
  /** P1-5：同一分支组的全部版本（含已 aborted 的），按 id 升序 */
  siblings?: Msg[];
  onSwitch?: (targetId: number) => void;
  onContinue?: () => void;
  onDiscard?: () => void;
  /** 有流在跑：停止卡按钮置灰 */
  busy?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const html = useMemo(() => renderMarkdown(m.content || ""), [m.content]);
  // 切换器位置：siblings 由 Task 层按 id 升序给
  const pos = siblings && siblings.length > 1 ? siblings.findIndex((s) => s.id === m.id) + 1 : 0;

  if (m.role === "system") {
    let d: Deliverable | null = null;
    try {
      d = JSON.parse(m.content);
    } catch {
      d = null;
    }
    if (d && d.type === "deliverable") {
      return (
        <article className="msg msg-system">
          <div className="msg-label">交付完成</div>
          <DeliverableCard d={d} />
        </article>
      );
    }
    return null;
  }

  if (m.role === "user") {
    // 工具结果回注也是 user 消息，但用户没说过这些话——按段渲染：
    // 纯回注消息换成中性卡片与「工具结果」标签，别冒充用户气泡。
    const segs = splitToolResults(m.content || "");
    const pureTool = segs.length > 0 && segs.every((s) => s.kind === "tool");
    return (
      <article
        className="msg msg-user"
        data-testid="msg-user"
        data-pure-tool={pureTool || undefined}
      >
        <div className="msg-label">
          {pureTool ? "工具结果" : "你"} · {formatTime(m.created_at)}
          {m.plan_flag && <span className="badge" style={{ marginLeft: 6 }}>计划</span>}
        </div>
        <div className="msg-body">
          {segs.map((s, i) =>
            s.kind === "text" ? (
              <span key={i}>{s.text}</span>
            ) : (
              <ToolResultCard key={i} block={s} />
            ),
          )}
        </div>
      </article>
    );
  }

  const usage = m.tokens_in || m.tokens_out
    ? `in ${m.tokens_in} · out ${m.tokens_out} · 缓存命中 ${m.cache_read}`
    : "";

  async function copyAll() {
    try {
      await navigator.clipboard.writeText(m.content || "");
      setCopied(true);
      toast("已复制");
      setTimeout(() => setCopied(false), 1200);
    } catch {
      toast("复制失败，请手动选择", "error");
    }
  }

  return (
    <article
      className={`msg msg-ai${m.status === "stopped" ? " msg-stopped" : ""}`}
      data-testid="msg-ai"
      data-msg-id={m.id}
    >
      <div className="msg-label">
        DA的AI
        {m.status === "stopped" && <span className="badge" style={{ marginLeft: 6 }}>已停止</span>}
      </div>
      {pos > 0 && siblings && onSwitch && (
        <div className="branch-switch" data-testid="branch-switch">
          <button
            className="btn btn-ghost btn-xs"
            title="上一版"
            data-testid="branch-prev"
            disabled={pos <= 1}
            onClick={() => onSwitch(siblings[pos - 2].id)}
          >
            ←
          </button>
          <span className="tiny faint" data-testid="branch-pos">
            第 {pos}/{siblings.length} 版
          </span>
          <button
            className="btn btn-ghost btn-xs"
            title="下一版"
            data-testid="branch-next"
            disabled={pos >= siblings.length}
            onClick={() => onSwitch(siblings[pos].id)}
          >
            →
          </button>
        </div>
      )}
      <div className="msg-tools">
        <button className="btn btn-ghost btn-xs" title="复制正文" onClick={() => void copyAll()}>
          {copied ? "已复制" : "复制"}
        </button>
        {isLastAi && onRegenerate && (
          <button className="btn btn-ghost btn-xs" title="重新生成" data-testid="regen-btn" onClick={onRegenerate}>
            重新生成
          </button>
        )}
      </div>
      {m.thinking ? (
        <details className="thinking" data-testid="thinking">
          <summary>思考 {m.thinking.length} 字 · 点击展开</summary>
          <div className="thinking-body">{m.thinking}</div>
        </details>
      ) : null}
      <CodeCopy html={html} />
      {m.status === "stopped" && onContinue && onDiscard && (
        <StoppedRun
          kept={(m.content || "").length}
          busy={!!busy}
          onContinue={onContinue}
          onDiscard={onDiscard}
        />
      )}
      {m.web_sources && m.web_sources.length > 0 && (
        <div className="source-list" data-testid="source-list">
          <div className="tiny faint" style={{ marginBottom: 4 }}>
            来源 {m.web_sources.length}
          </div>
          {m.web_sources.map((s) => (
            <a
              key={s.n}
              className="source-item"
              href={s.url}
              target="_blank"
              rel="noreferrer noopener"
              title={s.url}
            >
              <span className="source-n">[{s.n}]</span>
              <span className="grow truncate">{s.title || s.url}</span>
              {s.provider && <span className="tiny faint">{s.provider}</span>}
            </a>
          ))}
        </div>
      )}
      {usage && <div className="usage-line">{usage}</div>}
    </article>
  );
}

/** 文件预览弹层（材料「看」按钮）。 */
export async function previewFile(fileId: string) {
  try {
    const data = await api<{ name: string; meta: Record<string, unknown>; text_head?: string }>(
      `/api/files/${fileId}/preview`,
    );
    if ((data.meta as { image?: boolean })?.image) {
      openModal({
        title: data.name,
        wide: true,
        body: (
          <img
            src={`/api/files/${fileId}`}
            alt={data.name}
            style={{ maxWidth: "100%", maxHeight: "56vh", display: "block", margin: "0 auto", borderRadius: 10 }}
          />
        ),
        actions: [{ label: "关闭" }],
      });
      return;
    }
    openModal({
      title: data.name,
      wide: true,
      body: (
        <>
          <div className="tiny faint" style={{ marginBottom: 12 }}>
            {JSON.stringify(data.meta)}
          </div>
          <pre style={{ maxHeight: "52vh", overflow: "auto", whiteSpace: "pre-wrap" }}>{data.text_head || "没有抽取到文本"}</pre>
        </>
      ),
      actions: [{ label: "关闭" }],
    });
  } catch (err) {
    toast((err as Error).message, "error");
  }
}

export { confirmDialog };
