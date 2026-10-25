/**
 * 任务页：消息流 + SSE 对话/重生成 + 阶段轨 + 材料/交付物 + 生成流程 + 审批卡。
 * 承接首页 pendingDispatch：进页若有待派发消息自动发出。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router";
import Composer, { type ComposerParams, type SendPayload } from "../components/Composer";
import ErrorBoundary from "../components/ErrorBoundary";
import { DeliverableCard, Message, previewFile, type Deliverable, type Msg } from "../components/Message";
import { SkeletonList } from "../components/Skeleton";
import StateDot from "../components/StateDot";
import ToolTimeline from "../components/chat/ToolTimeline";
import ThinkingBlock from "../components/chat/ThinkingBlock";
import ApprovalCard, { type ApprovalDetail, type ApprovalState } from "../components/chat/ApprovalCard";
import ArtifactCard from "../components/chat/ArtifactCard";
import MemoryChips, { type MemoryOp } from "../components/chat/MemoryChips";
import ErrorState, { type ErrorSeverity } from "../components/chat/ErrorState";
import ContextPressure, { ctxFromMeta, type ContextCounts } from "../components/ContextPressure";
import { api, streamSSE, uploadOne } from "../lib/api";
import { renderMarkdown } from "../lib/md";
import { createStreamBatcher } from "../lib/streamBuffer";
import type { ToolKind } from "../lib/toolKind";
import { confirmDialog, openModal, reportClientError, takePendingDispatch, toast, useSession } from "../store/app";
import { Slot } from "../plugins/slots";

interface TaskDetail {
  task: {
    id: string;
    title: string;
    goal: string;
    mode: string;
    status: string;
    params?: {
      model?: string;
      effort?: string;
      prompt_skill_ids?: string[];
      word_count?: number;
      format?: Record<string, unknown>;
      web_search?: number | boolean;
      rag_collection_ids?: string[];
      tool_names?: string[];
      [k: string]: unknown;
    };
  };
  nodes: Array<{ key: string; label: string; status: string; attempt: number; error?: string }>;
  files: Array<{ id: string; kind: string; name: string; size: number; meta: Record<string, unknown> }>;
  messages: Msg[];
  approval?: { title: string; subtitle: string; slides: Array<{ index: number; title: string; layout: string }> } | null;
  tool_pending?: ToolPending | null;
}

const FLOW_LABELS: Record<string, string> = {
  plan: "拆纲",
  research: "资料检索",
  content: "逐页内容",
  render: "渲染 PPTX",
  validate: "校验",
  deliver: "交付",
};

interface LiveTool {
  name: string;
  status: string;
  input?: Record<string, unknown>;
  error?: string;
  /** 同一次调用的 running/waiting/done 共用；旧后端没有，退回按 name 合并 */
  call_id?: string;
  /** 后端算的语义分类；旧后端没有，前端按 name 兜底 */
  kind?: ToolKind;
  label?: string;
  ts?: number;
  seq?: number;
  /** P1-7：流式阶段已写出的参数字符数（后端边收边报） */
  chars?: number;
  /** P1-7：这个卡是不是「模型正在写参数」的流式卡（工具执行前） */
  streaming_args?: boolean;
}

interface ToolPending {
  name: string;
  input: Record<string, unknown>;
  reason?: string;
  label?: string;
  kind?: ToolKind;
  risk?: "high" | "normal";
  details?: ApprovalDetail[];
}

interface Live {
  thinking: string;
  answer: string;
  error: string;
  errorSeverity: ErrorSeverity;
  errorRetryable: boolean;
  modelUsed: string;
  images: number;
  imagesDropped: number;
  usage: string;
  sources: Array<{ n: number; title: string; url: string; provider?: string }>;
  tools: LiveTool[];
  toolPending: ToolPending | null;
  resumed: string;
  /** P1-8：meta 事件带来的六段上下文计数（压力条数据源） */
  ctx: ContextCounts | null;
  /** P1-7：done 事件带来的制品 id（这一轮建出的制品） */
  artifactIds: string[];
  /** P1-9：done 事件带来的记忆操作结果（chips 数据源） */
  memoryOps: MemoryOp[];
}

function emptyLive(): Live {
  return {
    thinking: "",
    answer: "",
    error: "",
    errorSeverity: "error",
    errorRetryable: true,
    modelUsed: "",
    images: 0,
    imagesDropped: 0,
    usage: "",
    sources: [],
    tools: [],
    toolPending: null,
    resumed: "",
    ctx: null,
    artifactIds: [],
    memoryOps: [],
  };
}

/**
 * 把一个 SSE 事件应用到 live 状态（纯函数，无副作用）。
 *
 * 从 setLive 更新器里抽出来的：更新器必须是纯的（StrictMode 下会跑两次），
 * 而原来的 meta 分支在里面夹带 toast/persistParams 副作用。模型回退的
 * 提示与落盘通过 onModelFallback 回调交还给调用方（事件上下文里执行）。
 */
function applyLiveEvent(
  cur: Live,
  event: string,
  data: Record<string, unknown>,
  onModelFallback?: (requested: string, used: string) => void,
): Live {
  const next = { ...cur };
  if (event === "thinking") next.thinking = cur.thinking + String(data.t || "");
  else if (event === "text") next.answer = cur.answer + String(data.t || "");
  else if (event === "meta") {
    next.modelUsed = String(data.model || "");
    if (data.model_fallback) onModelFallback?.(String(data.model_requested || ""), String(data.model || ""));
    next.images = Number(data.images || 0);
    next.imagesDropped = Number(data.images_dropped || 0);
    next.ctx = ctxFromMeta(data);
  } else if (event === "search") {
    next.sources = Array.isArray(data.sources) ? (data.sources as Live["sources"]) : [];
  } else if (event === "done") {
    // done 走合帧器的立即 flush（见 streamBuffer 契约），到这里事件已齐。
    // artifacts/memory_ops 旧后端不带，按空数组兜底。
    next.artifactIds = Array.isArray(data.artifacts) ? (data.artifacts as string[]) : [];
    next.memoryOps = Array.isArray(data.memory_ops)
      ? (data.memory_ops as MemoryOp[]).filter((op) => op && typeof op.op === "string")
      : [];
  } else if (event === "tool") {
    const name = String(data.name || "");
    const status = String(data.status || "");
    const callId = data.call_id ? String(data.call_id) : "";
    // 按 call_id 合并：一次调用的 running/waiting/done 三个事件合成一张卡。
    // 同一个工具一轮里可能被调多次（两轮都调 web_search），按 name 合并会把
    // 两次叠成一张、后一次覆盖前一次——call_id 就是为这个分的。
    // 旧后端没带 call_id：退回按 name 合并（且只认没有 call_id 的旧卡）。
    const existing = next.tools.findIndex((t) =>
      callId ? t.call_id === callId : t.name === name && !t.call_id,
    );
    const patch: Partial<LiveTool> = { status };
    // input/error 只在事件真带了的时候覆盖：done 事件不带 input，
    // 无条件展开会把 running 时记下的参数又抹掉。
    if (data.input && typeof data.input === "object") {
      patch.input = data.input as Record<string, unknown>;
    }
    if (data.error) patch.error = String(data.error);
    if (callId) patch.call_id = callId;
    if (data.kind) patch.kind = String(data.kind) as ToolKind;
    if (data.label) patch.label = String(data.label);
    if (typeof data.ts === "number") patch.ts = data.ts;
    if (typeof data.seq === "number") patch.seq = data.seq;
    // P1-7：流式阶段上报的「已写出字符数」与「这是流式卡」标记
    if (typeof data.chars === "number") patch.chars = data.chars;
    if (data.streaming_args) patch.streaming_args = true;
    if (existing >= 0) {
      next.tools = next.tools.map((t, i) => (i === existing ? { ...t, ...patch } : t));
    } else {
      next.tools = [...next.tools, { name, ...patch } as LiveTool];
    }
    if (status === "waiting") {
      // approval 事件先到、waiting 后到：后者别把前者的 label/risk/details 抹掉
      const keep = cur.toolPending && cur.toolPending.name === name ? cur.toolPending : null;
      next.toolPending = {
        ...(keep || {}),
        name,
        input: (data.input as Record<string, unknown>) || keep?.input || {},
      } as ToolPending;
    }
  } else if (event === "approval") {
    const details = Array.isArray(data.details)
      ? (data.details as ApprovalDetail[]).filter(
          (d) => d && typeof d.label === "string" && typeof d.value === "string",
        )
      : [];
    next.toolPending = {
      name: String(data.name || ""),
      input: (data.input as Record<string, unknown>) || {},
      reason: data.reason ? String(data.reason) : undefined,
      label: data.label ? String(data.label) : undefined,
      kind: data.kind ? (String(data.kind) as ToolKind) : undefined,
      risk: data.risk === "high" ? "high" : "normal",
      details,
    };
  } else if (event === "usage") {
    const bits = [next.modelUsed, `in ${data.in}`, `out ${data.out}`, `缓存命中 ${data.cache_read}`].filter(Boolean);
    if (data.images) bits.push(`图 ${data.images}`);
    next.usage = bits.join(" · ");
  } else if (event === "error") {
    next.error = String(data.message || "调用失败");
    // 旧后端不带这两个字段：按 error + 可重试兜底（重试走 regenerate，无害）
    next.errorSeverity = (data.severity as ErrorSeverity) || "error";
    next.errorRetryable = data.retryable === undefined ? true : Boolean(data.retryable);
  }
  return next;
}

interface ArtifactRowData {
  id: string;
  title: string;
  version: number;
  created_at: string;
  /** P1-7：制品正文字符数（后端按 LENGTH(html) 给，不传就不显示字数） */
  chars?: number;
}

export default function Task() {
  const { id = "" } = useParams();
  const { meta, me, refreshTasks } = useSession();
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [loadError, setLoadError] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [live, setLive] = useState<Live | null>(null);
  const [genBusy, setGenBusy] = useState(false);
  const [artifacts, setArtifacts] = useState<ArtifactRowData[]>([]);
  // 审批卡状态机：request → running → done/denied（组件内驱动，SSE 只给 request）
  const [approvalState, setApprovalState] = useState<ApprovalState>("request");
  // 错误重试计数：同一个错误连点上限 3 次，到量收按钮
  const [retryAttempt, setRetryAttempt] = useState(0);
  // P1-5：分支回看。key=分组键（parent_id ?? id），value={看的版本, 当时组内最新 id}
  const [branchPick, setBranchPick] = useState<Record<number, { id: number; max: number }>>({});
  // P1-8：最近一轮的上下文六段计数。live 清空后压力条还要在（下一轮开写
  // 前就该看见上一轮有多满），所以单独存一份，不随 live 销毁。
  const [lastCtx, setLastCtx] = useState<ContextCounts | null>(null);
  const ctrlRef = useRef<AbortController | null>(null);
  /** 每发起一次请求递增：回调靠它判断自己是不是还在给当前会话干活 */
  const runSeqRef = useRef(0);
  /** 延迟任务统一登记，卸载时一次清干净（原先散落的 setTimeout 无人回收） */
  const timersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());
  const bottomRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const reload = useCallback(async () => {
    try {
      const d = await api<TaskDetail>(`/api/tasks/${id}`);
      setDetail(d);
    } catch (err) {
      setLoadError((err as Error).message);
    }
    api<{ artifacts: ArtifactRowData[] }>(`/api/artifacts?task_id=${id}`)
      .then((d) => setArtifacts(d.artifacts || []))
      .catch(() => setArtifacts([]));
  }, [id]);

  const scrollToBottom = useCallback((force = false) => {
    requestAnimationFrame(() => {
      const el = listRef.current;
      if (!el) return;
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      if (force || distance < 320) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    });
  }, []);

  // 切会话：/t/:id 是同一路由，组件实例被复用，旧任务的 SSE 不会自动停。
  // 这里让它彻底作废——断连接、标记过期、清掉残留的 live 视图。
  useEffect(() => {
    runSeqRef.current += 1;
    ctrlRef.current?.abort();
    ctrlRef.current = null;
    setStreaming(false);
    setLive(null);
  }, [id]);

  // 离开页面：掐断连接 + 回收所有延迟任务
  useEffect(() => () => {
    ctrlRef.current?.abort();
    ctrlRef.current = null;
    timersRef.current.forEach((t) => clearTimeout(t));
    timersRef.current.clear();
  }, []);

  useEffect(() => {
    void reload();
    void refreshTasks();
  }, [reload, refreshTasks]);

  useEffect(() => {
    scrollToBottom(true);
  }, [detail?.messages.length, scrollToBottom]);

  // markdown 代码块复制按钮（流式渲染后统一补挂）
  useEffect(() => {
    const host = listRef.current;
    if (!host) return;
    host.querySelectorAll<HTMLElement>(".md pre").forEach((pre) => {
      if (pre.querySelector(".pre-copy")) return;
      const btn = document.createElement("button");
      btn.className = "pre-copy";
      btn.type = "button";
      btn.textContent = "复制";
      btn.onclick = async () => {
        try {
          await navigator.clipboard.writeText(pre.innerText.replace(/^复制\n?/, ""));
          btn.textContent = "已复制";
          setTimeout(() => (btn.textContent = "复制"), 1200);
        } catch {
          btn.textContent = "失败";
        }
      };
      pre.appendChild(btn);
    });
  }, [detail, live]);

  const persistParams = useCallback(
    (patch: Partial<ComposerParams>) => {
      const params: Record<string, unknown> = {};
      if (patch.model !== undefined) params.model = patch.model;
      if (patch.effort !== undefined) params.effort = patch.effort;
      if (patch.planMode !== undefined) params.plan_mode = patch.planMode ? 1 : 0;
      if (patch.skillIds !== undefined) params.prompt_skill_ids = patch.skillIds;
      if (patch.webSearch !== undefined) params.web_search = patch.webSearch ? 1 : 0;
      if (patch.ragIds !== undefined) params.rag_collection_ids = patch.ragIds;
      if (patch.toolNames !== undefined) params.tool_names = patch.toolNames;
      void api(`/api/tasks/${id}`, { method: "PATCH", body: { params } }).catch((err) => toast((err as Error).message, "error"));
      setDetail((d) => (d ? { ...d, task: { ...d.task, params: { ...(d.task.params || {}), ...params } } } : d));
    },
    [id],
  );

  const chatStream = useCallback(
    (path: string, body: Record<string, unknown>) => {
      if (streaming) {
        toast("等当前回复结束", "error");
        return;
      }
      setStreaming(true);
      setLive(emptyLive());
      // 请求序号：切换会话时（/t/:id 同路由不重挂载，组件实例被复用），
      // 上一个任务的流还会回来数据；回调先比对序号，过期的一律丢弃，
      // 否则 A 的回答会渲染在 B 的对话流里，B 还会一直卡在"生成中"。
      const runId = (runSeqRef.current += 1);
      const stale = () => runId !== runSeqRef.current;
      // 合帧：一帧一次 setLive。approval/error/done/usage 不等帧（见 streamBuffer 契约）。
      const batcher = createStreamBatcher({
        onFlush(events) {
          if (stale()) return;
          setLive((prev) => {
            let cur = prev || emptyLive();
            for (const [event, data] of events) {
              cur = applyLiveEvent(cur, event, data, (requested, used) => {
                toast(`「${requested}」不可用，已改用 ${used}`, "error");
                persistParams({ model: "" });
              });
            }
            return cur;
          });
          // meta 的六段计数顺手存一份持久态：流结束后压力条不消失
          for (const [event, data] of events) {
            if (event === "meta") setLastCtx(ctxFromMeta(data));
          }
          scrollToBottom();
        },
      });
      ctrlRef.current = streamSSE(`/api/tasks/${id}/${path}`, body, {
        onEvent(event, data) {
          if (stale()) return;
          batcher.push(event, data);
        },
        onError(message) {
          if (stale()) return;
          batcher.flushNow();
          setLive((prev) => ({ ...(prev || emptyLive()), error: message }));
        },
        onEnd() {
          // 尾帧优先冲刷再作废——否则最后一段正文会留在队列里被丢掉
          batcher.flushNow();
          batcher.dispose();
          if (stale()) return;
          setStreaming(false);
          ctrlRef.current = null;
          const timer = setTimeout(() => {
            timersRef.current.delete(timer);
            // 等待工具审批时保留 live 卡片（审批卡挂在它上面）
            setLive((cur) => (cur?.toolPending ? cur : null));
            void reload();
            void refreshTasks();
          }, 400);
          timersRef.current.add(timer);
        },
      });
    },
    [id, streaming, persistParams, reload, refreshTasks, scrollToBottom],
  );
  // 首页派发：进页自动发首条
  useEffect(() => {
    if (!detail) return;
    const pending = takePendingDispatch(id);
    if (!pending) return;
    const hasMessages = detail.messages.filter((m) => m.role !== "system").length > 0;
    if (hasMessages) return;
    const body: Record<string, unknown> = { text: pending.text };
    if (pending.model) body.model = pending.model;
    if (pending.effort) body.effort = pending.effort;
    if (pending.planMode) body.plan_mode = true;
    if (pending.webSearch !== undefined) body.web_search = pending.webSearch;
    if (pending.ragIds?.length) body.rag_collection_ids = pending.ragIds;
    if (pending.toolNames?.length) body.tool_names = pending.toolNames;
    persistParams({
      planMode: pending.planMode,
      skillIds: pending.skillIds,
      model: pending.model,
      effort: pending.effort,
      webSearch: pending.webSearch,
      ragIds: pending.ragIds,
      toolNames: pending.toolNames,
    });
    setRetryAttempt(0);
    chatStream("chat", body);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail, id]);

  function handleSend(p: SendPayload) {
    // 用户主动发新消息：重试计数归零（错误重试的 attempt 不跨回合累计）
    setRetryAttempt(0);
    const body: Record<string, unknown> = { text: p.text };
    if (p.model) body.model = p.model;
    if (p.effort) body.effort = p.effort;
    if (p.planMode) body.plan_mode = true;
    if (meta?.web?.enabled) body.web_search = p.webSearch;
    if (p.ragIds.length) body.rag_collection_ids = p.ragIds;
    if (p.toolNames.length) body.tool_names = p.toolNames;
    chatStream("chat", body);
  }

  /** 工具审批：批准后续跑同步返回 resumed 正文。 */
  async function respondToolApproval(approved: boolean) {
    const pending = live?.toolPending || detail?.tool_pending;
    if (!pending) return;
    setApprovalState("running");
    try {
      const res = await api<{ ok: boolean; approved: boolean; name: string; resumed?: string }>(
        `/api/tasks/${id}/tool-approve`,
        { method: "POST", body: { name: pending.name, input: pending.input, approved } },
      );
      setDetail((d) => (d ? { ...d, tool_pending: null } : d));
      if (!approved) {
        setApprovalState("denied");
        toast("已拒绝该工具调用");
        setLive(null);
        await reload();
        return;
      }
      if (res.resumed) {
        setApprovalState("done");
        // 审批通过走的是普通 POST（非 SSE），等审批的那张卡收不到 done 事件，
        // 这里手动把它推到「完成」——不然刷新前它一直挂在「等待审批」。
        setLive((prev) => ({
          ...(prev || emptyLive()),
          toolPending: null,
          tools: (prev?.tools || []).map((t) =>
            t.name === res.name && t.status === "waiting" ? { ...t, status: "done" } : t,
          ),
          resumed: res.resumed || "",
        }));
        toast(`工具 ${res.name} 执行完毕，已续答`, "ok");
      }
      setTimeout(() => {
        setLive(null);
        void reload();
        void refreshTasks();
      }, 600);
    } catch (err) {
      setApprovalState("request");
      toast((err as Error).message, "error");
    }
  }

  function regenerate() {
    chatStream("regenerate", {});
  }

  /** P1-4：接续被停止的半截回复。走普通 SSE 通道，服务端会把半截正文作为
   *  第一段 text 推回来，直播视图从头就是完整的。 */
  function continueStopped() {
    chatStream("chat/continue", {});
  }

  /** P1-4：丢弃半截回复。非流式 POST，回来刷新即可。 */
  async function discardStopped() {
    try {
      await api(`/api/tasks/${id}/chat/continue`, { method: "POST", body: { discard: true } });
      toast("已丢弃这段半截回复");
      await reload();
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  /** P1-5：分支切换。pick 记下「看的是哪一版 + 当时组里最新 id」——
   *  组里来了新消息（max 变了）pick 就算过期，自动跳回最新版。 */
  function switchBranch(m: Msg, targetId: number) {
    const key = m.parent_id ?? m.id;
    const arr = groupOf.get(key) || [m];
    setBranchPick((p) => ({ ...p, [key]: { id: targetId, max: arr[arr.length - 1].id } }));
  }

  async function retryNode(key: string) {
    toast(`重跑 ${key}…`);
    streamSSE(`/api/tasks/${id}/nodes/${key}/retry`, {}, {
      onEvent(event, data) {
        if (event === "error") toast(String(data.message || "重跑失败"), "error");
      },
      onEnd() {
        setTimeout(() => void reload(), 300);
      },
    });
  }

  async function deleteFile(fileId: string) {
    if (!(await confirmDialog("删除这份材料？"))) return;
    await api(`/api/files/${fileId}`, { method: "DELETE" });
    await reload();
  }

  function openGenerate() {
    if (!detail) return;
    const isSlides = detail.task.mode === "slides";
    const fmt = (detail.task.params?.format || {}) as { body_font?: string; body_size_pt?: number; line_spacing_pt?: number };
    let docType = isSlides ? "pptx" : "docx";
    const run = (payload: Record<string, unknown>) => {
      setGenBusy(true);
      detail.nodes.forEach((n) => {
        if (n.status !== "done") n.status = "todo";
      });
      streamSSE(`/api/tasks/${id}/generate`, payload, {
        onEvent(event, data) {
          if (event === "node") {
            setDetail((d) => {
              if (!d) return d;
              let nodes = d.nodes;
              const found = nodes.find((n) => n.key === data.key);
              if (found) {
                nodes = nodes.map((n) =>
                  n.key === data.key ? { ...n, status: String(data.status), error: data.error ? String(data.error) : n.error } : n,
                );
              } else if (typeof data.key === "string" && data.key.startsWith("draft.c")) {
                nodes = [...nodes, { key: data.key, label: String(data.label || data.key), status: String(data.status), attempt: 0 }];
              }
              return { ...d, nodes };
            });
          } else if (event === "approval") {
            setDetail((d) => (d ? { ...d, approval: data as TaskDetail["approval"] } : d));
            void reload();
          } else if (event === "error") {
            toast(String(data.message || "生成失败"), "error");
          } else if (event === "result") {
            toast(data.ok ? "已生成" : String(data.error || "生成未完成"), data.ok ? "ok" : "error");
          }
        },
        onError(message) {
          toast(message, "error");
        },
        onEnd() {
          setGenBusy(false);
          setTimeout(() => void reload(), 500);
        },
      });
    };

    openModal({
      title: isSlides ? "生成 PPT" : "生成文档",
      body: (
        <div id="gen-form">
          <div className="field">
            <label>交付类型</label>
            <div className="row">
              <label className="row" style={{ margin: 0 }}>
                <input type="radio" name="doc_type" value="docx" defaultChecked={!isSlides} onChange={() => (docType = "docx")} /> Word 文档
              </label>
              <label className="row" style={{ margin: 0 }}>
                <input type="radio" name="doc_type" value="pptx" defaultChecked={isSlides} onChange={() => (docType = "pptx")} /> PPT 演示
              </label>
            </div>
          </div>
          <div className="field">
            <label htmlFor="word_count">目标字数（Word）</label>
            <input id="word_count" type="number" min={300} max={8000} step={100} defaultValue={detail.task.params?.word_count || 1500} />
          </div>
          <div className="field">
            <label htmlFor="page_count">页数（PPT）</label>
            <input id="page_count" type="number" min={4} max={12} defaultValue={6} />
          </div>
          <div className="field">
            <label htmlFor="gen-effort">思考深度</label>
            <select id="gen-effort" defaultValue={me?.pref_effort || ""}>
              {meta?.efforts.map((k) => (
                <option key={k} value={k}>
                  {meta.effort_labels[k] || k}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>正文格式（Word）</label>
            <div className="row">
              <select id="font_name" defaultValue={fmt.body_font || "宋体"}>
                <option>宋体</option>
                <option>仿宋</option>
                <option>楷体</option>
              </select>
              <select id="font_size" defaultValue={String(fmt.body_size_pt || 10.5)}>
                <option value="10.5">五号 10.5pt</option>
                <option value="12">小四 12pt</option>
                <option value="9">小五 9pt</option>
              </select>
              <select id="line_spacing" defaultValue={String(fmt.line_spacing_pt || 22)}>
                <option value="22">固定 22 磅</option>
                <option value="28">固定 28 磅</option>
                <option value="18">固定 18 磅</option>
              </select>
            </div>
          </div>
        </div>
      ),
      actions: [
        { label: "取消" },
        {
          label: "开始生成",
          kind: "btn-primary",
          onClick: (close) => {
            const el = document.getElementById("gen-form")!;
            const val = (id2: string) => (el.querySelector(`#${id2}`) as HTMLInputElement)?.value;
            const payload: Record<string, unknown> = {
              doc_type: docType,
              effort: val("gen-effort"),
              word_count: Number(val("word_count")) || 1500,
              page_count: Number(val("page_count")) || 6,
            };
            if (docType === "docx") {
              const size = Number(val("font_size"));
              const format = {
                body_font: val("font_name"),
                body_size_pt: size,
                line_spacing_pt: Number(val("line_spacing")),
                first_line_indent_pt: size * 2,
              };
              payload.format = format;
              void api(`/api/tasks/${id}`, { method: "PATCH", body: { params: { format, word_count: payload.word_count } } }).catch(() => undefined);
            }
            close();
            run(payload);
          },
        },
      ],
    });
  }

  async function runFlow(path: string, body: Record<string, unknown>, busyLabel: string) {
    if (genBusy) {
      toast("等当前流程结束", "error");
      return;
    }
    setGenBusy(true);
    toast(busyLabel);
    streamSSE(`/api/tasks/${id}/${path}`, body, {
      onEvent(event, data) {
        if (event === "node") {
          setDetail((d) =>
            d
              ? {
                  ...d,
                  nodes: d.nodes.map((n) => (n.key === data.key ? { ...n, status: String(data.status) } : n)),
                }
              : d,
          );
        } else if (event === "error") {
          toast(String(data.message || "流程失败"), "error");
        } else if (event === "result") {
          toast(data.ok ? "已完成" : String(data.error || "未完成"), data.ok ? "ok" : "error");
        }
      },
      onError(message) {
        toast(message, "error");
      },
      onEnd() {
        setGenBusy(false);
        setTimeout(() => void reload(), 400);
      },
    });
  }

  async function openRevise() {
    let feedback = "";
    openModal({
      title: "提出修改",
      body: (
        <div className="field">
          <label htmlFor="feedback">修改意见</label>
          <textarea id="feedback" rows={3} placeholder="例：第 3 页换成数据对比，讲稿再口语一点" onChange={(e) => (feedback = e.target.value)} />
        </div>
      ),
      actions: [
        { label: "取消" },
        {
          label: "提交修改",
          kind: "btn-primary",
          onClick: (close) => {
            if (!feedback.trim()) return;
            close();
            void runFlow("revise", { feedback }, "修改中");
          },
        },
      ],
    });
  }

  // 新审批到达 → 状态机回到 request（approval SSE 只负责「来活了」，
  // running/done/denied 都由本组件驱动）。
  // 必须放在下面两个早退（loadError / !detail）之前：挂在 detail 判空之后
  // 就是条件 hook，detail 从 null 变非 null 的那次渲染直接 React #310。
  const pendingName = (live?.toolPending || detail?.tool_pending)?.name || "";
  useEffect(() => {
    if (pendingName) setApprovalState("request");
  }, [pendingName]);

  if (loadError) {
    return (
      <div className="page">
        <div className="empty">
          <h4>这个会话没能打开</h4>
          <p>{loadError}</p>
        </div>
      </div>
    );
  }
  if (!detail || !meta) {
    return (
      <div className="page">
        <SkeletonList rows={3} />
      </div>
    );
  }

  const { task, nodes, files, messages, approval } = detail;
  const materials = files.filter((f) => f.kind === "source");
  const deliverables = files.filter((f) => f.kind === "deliverable");
  // P1-4：被停止的半截回复也进可见列表（下面挂停止卡）；流式进行中先收起来——
  // 继续写时旧半截与新直播会同时出现，重复且碍眼，等流结束再露。
  // P1-5：同一分支组（assistant 的 parent_id ?? id 相同）只显示一条。
  // 分组覆盖**全部** assistant（含已 aborted 的）：旧版本不显示，却是切换器的
  // 数据源——只分 done/stopped 的话旧版根本不进组，← → 永远只有一版。
  // 默认显示最新版；用户切过去的旧版哪怕是 aborted 也照显（回看就是干这个的）。
  // 分组按首次出现排序：重生成的新消息 id 在末尾，但仍显示在旧版的位置上。
  const visible: Msg[] = [];
  const groupOf = new Map<number, Msg[]>();
  for (const m of messages) {
    if (m.role !== "assistant") {
      groupOf.set(m.id, [m]);
      continue;
    }
    const key = m.parent_id ?? m.id;
    const arr = groupOf.get(key);
    if (arr) arr.push(m);
    else groupOf.set(key, [m]);
  }
  for (const [key, arr] of groupOf) {
    if (arr[0].role !== "assistant") {
      visible.push(arr[0]);
      continue;
    }
    const shown = arr.filter((m) => m.status === "done" || (m.status === "stopped" && !streaming));
    if (!shown.length) continue; // 整组都让位了（丢弃/空停止）→ 这条位置不显示
    const maxId = arr[arr.length - 1].id;
    const pick = branchPick[key];
    const picked = pick && pick.max === maxId ? arr.find((m) => m.id === pick.id) : null;
    // pick 指向 stopped 且正在流式中：默认项都不显示它，pick 也不该显
    const active = picked && (!streaming || picked.status !== "stopped") ? picked : shown[shown.length - 1];
    visible.push(active);
  }
  const lastAiId = [...visible].reverse().find((m) => m.role === "assistant")?.id;
  const initialParams: Partial<ComposerParams> = {
    model: task.params?.model || "",
    effort: task.params?.effort || "",
    planMode: Boolean(task.params?.plan_mode),
    skillIds: task.params?.prompt_skill_ids || [],
    webSearch: task.params?.web_search === undefined ? true : Boolean(task.params.web_search),
    ragIds: task.params?.rag_collection_ids || [],
    toolNames: task.params?.tool_names || [],
  };
  const showApproval = task.status === "waiting_approval" && approval;
  const rawPending = live?.toolPending || detail.tool_pending || null;
  // 防御：历史数据里 tool_pending 可能是 {} 或缺 name，那种不算待审批
  const toolPending = rawPending && rawPending.name ? rawPending : null;
  // P1-7：制品生成中（artifact_create 工具执行中）→ 直播卡显示实时字数
const artTool = live?.tools.find((t) => t.name === "artifact_create");
// P1-7 generating 态的判据：**后端流式上报的围栏状态**。
//
// 原来盯的是工具卡的 status，但后端整轮收完文本才解析围栏，running 事件
// 要等围栏闭合才发，窗口只有几毫秒。现在后端边收边报（流式围栏观察），
// 工具卡一开就是「正在写」，chars 就是已写出的字符数。
//
// 兼容旧后端：没有 streaming_args 标记时退回原来的 status 判据。
const artStreaming = !!artTool?.streaming_args && artTool.status !== "done" && artTool.status !== "error";
const artGenerating = artStreaming
  || (!!artTool && !artTool.streaming_args && artTool.status !== "done" && artTool.status !== "error");
const artWords = artTool?.chars ?? live?.answer.length ?? 0;

  return (
    <div className={`task-grid${nodes.length === 0 ? " no-rail" : ""}`}>
      {nodes.length > 0 && (
        <aside className="task-rail" data-testid="node-rail">
          <div className="rail-title">阶段</div>
          {nodes.map((n) => (
            <div
              key={n.key}
              className={`node ${n.status}`}
              title={n.error || ""}
              style={n.status === "failed" ? { cursor: "pointer" } : undefined}
              onClick={() => n.status === "failed" && void retryNode(n.key)}
            >
              <span className="node-dot" />
              <span>{FLOW_LABELS[n.key] || n.label}</span>
              {n.attempt > 1 && <span className="node-detail">重试 {n.attempt - 1} 次</span>}
            </div>
          ))}
          {nodes.some((n) => n.status === "failed") && <div className="badge badge-warn">有失败节点，可单点重跑</div>}
        </aside>
      )}

      <section className="stream">
        <div className="task-head">
          <div className="row-between">
            <h2 data-testid="task-title">{task.title}</h2>
            <div className="row" style={{ gap: 6 }}>
              {/* 插件挂载点：chat.header.actions */}
              <Slot name="chat.header.actions" messages={visible} task={task} />
              <button
              className="btn btn-ghost btn-sm"
              onClick={() => {
                let goal = task.goal;
                openModal({
                  title: "改目标",
                  body: (
                    <div className="field">
                      <label htmlFor="goal2">目标描述</label>
                      <textarea id="goal2" rows={4} defaultValue={task.goal} onChange={(e) => (goal = e.target.value)} />
                    </div>
                  ),
                  actions: [
                    { label: "取消" },
                    {
                      label: "保存",
                      kind: "btn-primary",
                      onClick: async (close) => {
                        if (goal.trim().length < 2) return;
                        close();
                        await api(`/api/tasks/${id}`, { method: "PATCH", body: { goal } });
                        await reload();
                      },
                    },
                  ],
                });
              }}
            >
              改目标
            </button>
            </div>
          </div>
          {task.goal && !task.goal.startsWith(task.title.replace(/[…\s]+$/, "")) && <div className="task-goal">{task.goal}</div>}
        </div>

        <div ref={listRef} className="msg-list" data-testid="msg-list" style={{ flex: 1, overflowY: "auto" }}>
          {visible.length === 0 && !live && (
            <div className="empty" style={{ marginTop: 24 }}>
              <h4>还没有对话</h4>
              <p>在下方写一句要求，或直接说「开始写」。</p>
            </div>
          )}
          <ErrorBoundary
            label="历史消息"
            onCaughtError={(err, info) => reportClientError(err, String(info.componentStack || ""), "task-messages")}
          >
            {visible.map((m) => (
              <Message
                key={m.id}
                m={m}
                isLastAi={m.id === lastAiId}
                onRegenerate={regenerate}
                // 组按「分组键」存（parent_id ?? id），查也得用键——
                // 按 m.id 查只有组根能命中，续跑/重生成出来的非根消息全查不到
                siblings={groupOf.get(m.parent_id ?? m.id)}
                onSwitch={(targetId) => switchBranch(m, targetId)}
                onContinue={continueStopped}
                onDiscard={() => void discardStopped()}
                busy={streaming}
              />
            ))}
          </ErrorBoundary>

          {live && (
            <article className="msg msg-ai" data-testid="live-msg">
              <div className="msg-label">DA的AI</div>
              <ThinkingBlock text={live.thinking} streaming={streaming} hasAnswer={!!live.answer} />
              {live.images > 0 && (
                <div className="usage-line">
                  携带图片 {live.images} 张{live.imagesDropped ? ` · ${live.imagesDropped} 张未读取` : ""}
                </div>
              )}
              {live.tools.length > 0 && <ToolTimeline tools={live.tools} />}
              {artGenerating && <ArtifactCard state="generating" words={artWords} />}
              <div className={`msg-body md ${live.answer ? "" : "cursor"}`} dangerouslySetInnerHTML={{ __html: live.answer ? renderMarkdown(live.answer) : "" }} />
              {live.resumed && (
                <div className="msg-body md" data-testid="resumed-answer" dangerouslySetInnerHTML={{ __html: renderMarkdown(live.resumed) }} />
              )}
              {live.memoryOps.length > 0 && <MemoryChips ops={live.memoryOps} />}
              {live.sources.length > 0 && (
                <div className="source-list" data-testid="live-sources">
                  <div className="tiny faint" style={{ marginBottom: 4 }}>
                    来源 {live.sources.length}
                  </div>
                  {live.sources.map((s) => (
                    <a key={s.n} className="source-item" href={s.url} target="_blank" rel="noreferrer noopener" title={s.url}>
                      <span className="source-n">[{s.n}]</span>
                      <span className="grow truncate">{s.title || s.url}</span>
                      {s.provider && <span className="tiny faint">{s.provider}</span>}
                    </a>
                  ))}
                </div>
              )}
              {live.usage && <div className="usage-line">{live.usage}</div>}
              {live.error && (
                <ErrorState
                  message={live.error}
                  severity={live.errorSeverity}
                  retryable={live.errorRetryable}
                  attempt={retryAttempt}
                  busy={streaming}
                  onRetry={() => {
                    // 原地重试 = 重新生成最后一条（不新增 user 消息：
                    // 原样补发会把对话弄出重复轮次）
                    setRetryAttempt((a) => Math.min(a + 1, 3));
                    regenerate();
                  }}
                />
              )}
            </article>
          )}
          {/* 插件挂载点：chat.turn.tail */}
          <Slot name="chat.turn.tail" messages={visible} task={task} />
          <div ref={bottomRef} />
        </div>

        <div style={{ padding: "0 16px 16px" }}>
          {/* P1-8：上下文压力条——最近一轮的六段构成，流结束后也在，
              下一轮开写前就知道上一轮喂了多少东西进去 */}
          {lastCtx && <ContextPressure counts={lastCtx} />}
          {toolPending && !showApproval && (
            <ApprovalCard
              toolName={toolPending.name}
              label={toolPending.label}
              kind={toolPending.kind}
              risk={toolPending.risk}
              reason={toolPending.reason}
              details={toolPending.details || []}
              state={approvalState}
              busy={streaming || approvalState === "running"}
              onAllow={() => void respondToolApproval(true)}
              onDeny={() => void respondToolApproval(false)}
            />
          )}

          {showApproval && (
            <article className="msg msg-system approval-card" data-testid="approval-card">
              <div className="msg-label">渲染前确认 · 大纲已就绪</div>
              <div className="approval-title">{approval!.title || "未命名演示"}</div>
              {approval!.subtitle && <div className="approval-sub muted small">{approval!.subtitle}</div>}
              <ol className="approval-slides">
                {approval!.slides.map((s) => (
                  <li key={s.index}>
                    <span className="badge">{s.layout}</span> {s.title || "（未命名页）"}
                  </li>
                ))}
              </ol>
              <div className="approval-actions">
                <button className="btn btn-primary btn-sm" onClick={() => void runFlow("approve", {}, "渲染中")}>
                  确认，开始渲染
                </button>
                <button className="btn btn-ghost btn-sm" onClick={() => void openRevise()}>
                  提出修改
                </button>
                <button
                  className="btn btn-ghost btn-sm"
                  onClick={async () => {
                    if (!(await confirmDialog("放弃这份大纲？之后可以重新生成。"))) return;
                    try {
                      await api(`/api/tasks/${id}/cancel`, { method: "POST" });
                      await reload();
                    } catch (err) {
                      toast((err as Error).message, "error");
                    }
                  }}
                >
                  放弃大纲
                </button>
              </div>
              <div className="tiny faint" style={{ marginTop: 6 }}>
                确认后渲染不扣次数；提出修改会重写内容并计 1 次。
              </div>
            </article>
          )}

          <Composer
            meta={meta}
            placeholder={streaming ? "回复生成中…" : "补充要求，或直接说开始写"}
            streaming={streaming}
            onSend={handleSend}
            onStop={() => ctrlRef.current?.abort()}
            taskId={id}
            initial={initialParams}
            onParamsChange={persistParams}
            onUploaded={() => void reload()}
          />
        </div>
      </section>

      <aside className="task-aside">
        <div className="aside-block">
          <div className="aside-head">
            <span>材料</span>
            <span className="faint">{materials.length ? `${materials.length} 份` : ""}</span>
          </div>
          <div className="dropzone" onClick={() => document.getElementById("aside-file-input")?.click()}>
            添加材料
            <span className="dropzone-sub">
              {meta.upload.ext
                .filter((e) => !meta.upload.image_ext.includes(e))
                .map((e) => e.slice(1).toUpperCase())
                .join(" · ")}
              ，{meta.upload.max_mb}MB 以内
            </span>
          </div>
          <input
            id="aside-file-input"
            type="file"
            multiple
            hidden
            onChange={(e) => {
              // 复用 Composer 的上传逻辑：直接逐个上传
              const files2 = Array.from(e.target.files || []);
              e.target.value = "";
              void (async () => {
                for (const file of files2) {
                  try {
                    await uploadQuiet(id, file);
                  } catch (err) {
                    toast(`${file.name}：${(err as Error).message}`, "error");
                  }
                }
                await reload();
              })();
            }}
          />
          <div style={{ marginTop: 8 }}>
            {materials.length === 0 && <p className="tiny faint" style={{ margin: 0 }}>暂无材料</p>}
            {materials.map((f) => {
              const m = f.meta || {};
              const isImage = Boolean(m.image);
              const note = isImage ? "图片 · 多模态模型可读" : m.has_text ? `${m.page_count ? m.page_count + " 页 · " : ""}${m.chars || 0} 字` : ((m.note as string) || "无文本");
              return (
                <div key={f.id} className="file-row">
                  {isImage && <img className="file-thumb" src={`/api/files/${f.id}/thumb`} alt="" loading="lazy" />}
                  <span className="grow">
                    <span className="name truncate" title={f.name} style={{ display: "block" }}>
                      {f.name}
                    </span>
                    <span className="meta">{note}</span>
                  </span>
                  <button className="btn btn-ghost btn-xs" onClick={() => void previewFile(f.id)}>
                    {isImage ? "图" : "看"}
                  </button>
                  <button className="btn btn-ghost btn-xs" onClick={() => void deleteFile(f.id)}>
                    ×
                  </button>
                </div>
              );
            })}
          </div>
        </div>

        <div className="aside-block">
          <div className="aside-head">
            <span>交付物</span>
          </div>
          <button className="btn btn-primary" style={{ width: "100%" }} data-testid="generate-btn" disabled={genBusy} onClick={openGenerate}>
            {genBusy ? <StateDot status="running" /> : null} 生成文件
          </button>
          <div>
            {deliverables.length === 0 && <p className="tiny faint" style={{ marginTop: 8, marginBottom: 0 }}>暂无文件</p>}
            {[...deliverables].reverse().map((f) => (
              <DeliverableCard
                key={f.id}
                d={{ type: "deliverable", file_id: f.id, name: f.name, size: f.size, report: (f.meta?.report as Deliverable["report"]) || {} }}
              />
            ))}
          </div>
        </div>

        {artifacts.length > 0 && (
          <div className="aside-block">
            <div className="aside-head">
              <span>Artifacts</span>
              <span className="faint">{artifacts.length} 件</span>
            </div>
            {[...artifacts].reverse().map((a) => (
              <ArtifactCard
                key={a.id}
                state="done"
                title={a.title}
                artifactId={a.id}
                version={a.version}
                words={a.chars}
              />
            ))}
          </div>
        )}
      </aside>
    </div>
  );
}

/** 静默上传单个文件（材料区入口）。 */
async function uploadQuiet(taskId: string, file: File) {
  const { promise } = uploadOne(taskId, file);
  return promise;
}
