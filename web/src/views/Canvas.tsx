/**
 * 工作流画布（V4.0 N9）：8 类节点 + 属性面板 + 服务端校验高亮 + 运行时间线 + 审批续跑。
 * 引擎语义：start 唯一、波内并行、approval 暂停、resume 跳过已跑节点。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  addEdge,
  getBezierPath,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type ConnectionLineComponentProps,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import ErrorBoundary from "../components/ErrorBoundary";
import EmptyState from "../components/EmptyState";
import StateDot from "../components/StateDot";
import { checkConnection } from "../canvas/connectionRules";
import { KIND_META, type Kind } from "../canvas/kinds";
import { computeAutoLayout } from "../canvas/autoLayout";
import { applyRunStatus, emptyRunStatus, type RunStatusTable } from "../canvas/runStatus";
import { SCHEMA, cleanData, GATE_OPS, type FieldSpec } from "../canvas/schema";
import { useUndo } from "../hooks/useUndo";
import { api, streamSSE } from "../lib/api";
import { formatTime } from "../lib/format";
import { confirmDialog, openModal, reportClientError, toast, useSession, type Meta } from "../store/app";

interface NodeData extends Record<string, unknown> {
  kind: Kind;
  label?: string;
  [k: string]: unknown;
}

let idSeq = 1;
/** 新节点按已有节点排布（3 列网格），避免落到画布可视区之外。 */
function newNode(kind: Kind, existing: Array<Node<NodeData>>): Node<NodeData> {
  const idx = existing.length;
  return {
    id: `${kind}_${idSeq++}`,
    type: "da",
    position: { x: 140 + (idx % 3) * 330, y: 80 + Math.floor(idx / 3) * 180 },
    data: { kind, label: KIND_META[kind].label },
  };
}

/* ── 自定义节点渲染 ─────────────────────────────────── */
type DaNodeT = Node<NodeData>;

const BADGE_TEXT: Record<string, string> = {
  running: "运行中",
  done: "完成",
  failed: "失败",
  waiting: "待确认",
};

function DaNode({ data, selected }: NodeProps<DaNodeT>) {
  const kind = (data.kind || "start") as Kind;
  const meta = KIND_META[kind] || { label: kind, color: "#888" };
  const status = String(data.status || "");
  const runError = String(data.runError || "");
  const errors = (data.errors as string[] | undefined) || [];
  // 本轮运行已激活但还没跑到这个节点 → 置灰（不是隐藏），用户能看到完整图
  const dimmed = Boolean(data.unreached);
  const badge = status ? BADGE_TEXT[status] || status : "";
  return (
    <div
      className={`wf-node ${status} ${dimmed ? "_dimmed" : ""}`}
      data-testid="wf-node"
      data-node-kind={kind}
      style={{ borderColor: errors.length ? "#d64550" : selected ? meta.color : undefined }}
      title={errors.join("\n") || undefined}
    >
      {/* 状态徽标挂在节点框右上角外（P2-2）；只有 failed 才带错误 tooltip */}
      {badge && (
        <span
          className={`wf-badge wf-badge-${status}`}
          data-testid="wf-badge"
          data-badge-status={status}
          title={status === "failed" && runError ? runError : undefined}
        >
          {status === "running" && <StateDot status="running" />}
          {status === "done" && "✓"}
          {status === "failed" && "✗"}
          {badge}
        </span>
      )}
      {kind !== "start" && <Handle type="target" position={Position.Top} />}
      <div className="wf-node-head" style={{ background: meta.color }}>
        {meta.label}
      </div>
      <div className="wf-node-body truncate" title={String(data.label || "")}>
        {String(data.label || "")}
      </div>
      {errors.length > 0 && <div className="wf-node-err">{errors[0]}</div>}
      {kind !== "output" && kind !== "artifact" && <Handle type="source" position={Position.Bottom} />}
    </div>
  );
}
const nodeTypes = { da: DaNode };

/* ── 连线拖拽实时反馈（S2-2） ─────────────────────────── */
/**
 * 拖拽中的连线走向线。isValidConnection 判合法/非法，
 * xyflow 把结果通过 connectionStatus 传进来：合法走品牌色实线，
 * 非法走红虚线 + 末端一个红点——用户松手前就知道这条线建不建得起来。
 *
 * 移植来源：xyflow 官方 connectionLineComponent 范式（Flowise 同款用法）。
 */
function DaConnectionLine({
  fromX,
  fromY,
  toX,
  toY,
  fromPosition,
  toPosition,
  connectionStatus,
}: ConnectionLineComponentProps) {
  const [path] = getBezierPath({
    sourceX: fromX,
    sourceY: fromY,
    sourcePosition: fromPosition,
    targetX: toX,
    targetY: toY,
    targetPosition: toPosition,
  });
  const invalid = connectionStatus === "invalid";
  return (
    <g data-testid="wf-connection-line" data-connection-status={connectionStatus || "none"}>
      <path
        d={path}
        fill="none"
        stroke={invalid ? "var(--wf-failed)" : "var(--accent)"}
        strokeWidth={invalid ? 2 : 2.5}
        strokeDasharray={invalid ? "7 5" : undefined}
      />
      {invalid && <circle cx={toX} cy={toY} r={5} fill="var(--wf-failed)" />}
    </g>
  );
}

/* ── 属性面板（schema 驱动，P2-4） ────────────────────── */
function resolveOptions(
  spec: FieldSpec,
  meta: Meta | null,
  tools: Array<{ name: string; label: string }>,
): Array<{ value: string; label: string }> {
  if (spec.source === "models") {
    return (meta?.models || []).map((m) => ({ value: m.id, label: m.short || m.label }));
  }
  if (spec.source === "efforts") {
    return (meta?.efforts || []).map((k) => ({ value: k, label: meta?.effort_labels?.[k] || k }));
  }
  if (spec.source === "tools") {
    return tools.map((t) => ({ value: t.name, label: t.label || t.name }));
  }
  if (spec.source === "gate-ops") {
    return GATE_OPS.map((op) => ({ value: op, label: op }));
  }
  return [];
}

function Field({
  spec,
  value,
  options,
  onChange,
}: {
  spec: FieldSpec;
  value: unknown;
  options: Array<{ value: string; label: string }>;
  onChange: (v: unknown) => void;
}) {
  const str = value === undefined || value === null ? "" : String(value);
  if (spec.type === "string" || spec.type === "json" || spec.type === "array") {
    return (
      <div className="field" data-testid={`wf-field-${spec.key}`}>
        <label>{spec.label}</label>
        {spec.rows ? (
          <textarea
            rows={spec.rows}
            placeholder={spec.placeholder}
            value={str}
            onChange={(e) => onChange(e.target.value)}
          />
        ) : (
          <input
            placeholder={spec.placeholder}
            value={str}
            onChange={(e) => onChange(e.target.value)}
          />
        )}
        {spec.type === "json" && str && (() => {
          try {
            JSON.parse(str);
            return null;
          } catch {
            return <span className="tiny" style={{ color: "#d64550" }}>JSON 格式不对</span>;
          }
        })()}
        {spec.type === "array" && <span className="tiny faint">每行一项</span>}
      </div>
    );
  }
  if (spec.type === "number") {
    return (
      <div className="field" data-testid={`wf-field-${spec.key}`}>
        <label>{spec.label}</label>
        <input
          type="number"
          min={1}
          max={20}
          value={str || "6"}
          onChange={(e) => onChange(Number(e.target.value) || 6)}
        />
      </div>
    );
  }
  if (spec.type === "boolean") {
    return (
      <div className="field" data-testid={`wf-field-${spec.key}`}>
        <label className="row" style={{ gap: 6 }}>
          <input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />
          {spec.label}
        </label>
      </div>
    );
  }
  // options
  return (
    <div className="field" data-testid={`wf-field-${spec.key}`}>
      <label>{spec.label}</label>
      <select value={str} onChange={(e) => onChange(e.target.value)}>
        <option value="">默认</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

function PropPanel({
  node,
  meta,
  onChange,
  onDelete,
}: {
  node: Node<NodeData> | null;
  meta: Meta | null;
  onChange: (id: string, patch: Partial<NodeData>) => void;
  onDelete: (id: string) => void;
}) {
  const [tools, setTools] = useState<Array<{ name: string; label: string; enabled: boolean; builtin: boolean }>>([]);
  useEffect(() => {
    api<{ tools: Array<{ name: string; label: string; enabled: boolean; builtin: boolean }> }>("/api/tools")
      .then((d) => setTools((d.tools || []).filter((t) => t.builtin && t.enabled)))
      .catch(() => setTools([]));
  }, []);

  if (!node) {
    return (
      <div className="wf-props" data-testid="wf-props">
        <div className="aside-head">
          <span>属性</span>
        </div>
        <p className="tiny faint">点一个节点编辑它的参数。左侧色块添加节点，拖拽锚点连线。</p>
      </div>
    );
  }

  const d = node.data as NodeData;
  const set = (patch: Partial<NodeData>) => onChange(node.id, patch);
  const fields = SCHEMA[d.kind] || [];
  const visible = fields.filter((f) => !f.display || f.display(d));

  return (
    <div className="wf-props" data-testid="wf-props">
      <div className="aside-head">
        <span>{KIND_META[d.kind]?.label || d.kind}</span>
        <span className="tiny faint mono">{node.id}</span>
      </div>
      {visible.map((spec) => (
        <Field
          key={spec.key}
          spec={spec}
          value={d[spec.key]}
          options={resolveOptions(spec, meta, tools)}
          onChange={(v) => set({ [spec.key]: v } as Partial<NodeData>)}
        />
      ))}
      {d.kind !== "start" && (
        <button className="btn btn-ghost btn-sm" style={{ marginTop: 8 }} onClick={() => onDelete(node.id)}>
          删除节点
        </button>
      )}
    </div>
  );
}

/* ── 运行时间线条目 ─────────────────────────────────── */
interface LogLine {
  text: string;
  kind: string;
}

/* ── 主视图 ─────────────────────────────────────────── */
interface ServerNode {
  id: string;
  type?: string;
  position: { x: number; y: number };
  data?: Record<string, unknown>;
}
interface ServerGraph {
  nodes: ServerNode[];
  edges: Array<{ id: string; source: string; target: string }>;
}

interface WfRow {
  id: string;
  name: string;
  description: string;
  builtin: boolean;
  updated_at: string;
  graph: ServerGraph;
}

export default function Canvas() {
  return (
    <ReactFlowProvider>
      <CanvasInner />
    </ReactFlowProvider>
  );
}

function CanvasInner() {
  const { meta } = useSession();
  const rf = useReactFlow();
  /** 节点集合变化后把视口收到能看见全部节点（fitView 只在挂载时自动跑一次）。 */
  const refit = useCallback(() => {
    const t = setTimeout(() => {
      void rf.fitView({ duration: 200, padding: 0.3, maxZoom: 1.25 });
    }, 60);
    return () => clearTimeout(t);
  }, [rf]);
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<NodeData>>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [selectedId, setSelectedId] = useState("");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  // P2-1：运行状态走偏表，运行时绝不增删节点/边
  const [runStatuses, setRunStatuses] = useState<RunStatusTable>(emptyRunStatus);
  const [workflows, setWorkflows] = useState<WfRow[]>([]);
  const [current, setCurrent] = useState<WfRow | null>(null);
  const [wfName, setWfName] = useState("未命名工作流");
  const [running, setRunning] = useState(false);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [approval, setApproval] = useState<{ runId: string; nodeId: string; reason: string } | null>(null);
  const [output, setOutput] = useState("");

  // P2-4：图结构撤销/重做。apply 整体恢复节点与边。
  const applySnapshot = useCallback(
    (snap: { nodes: Node<NodeData>[]; edges: Edge[] }) => {
      setNodes(snap.nodes);
      setEdges(snap.edges);
      setSelectedId("");
    },
    [setNodes, setEdges],
  );
  useUndo(useMemo(() => ({ nodes, edges }), [nodes, edges]), applySnapshot);

  /** 渲染层：把偏表叠加进节点 data，节点集合本身不动（P2-1）。 */
  const displayNodes = useMemo(() => applyRunStatus(nodes, runStatuses), [nodes, runStatuses]);

  const paintList = useCallback(async () => {
    try {
      const d = await api<{ workflows: WfRow[] }>("/api/workflows");
      setWorkflows(d.workflows || []);
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }, []);
  useEffect(() => {
    void paintList();
  }, [paintList]);

  /** 当前图 → 连线规则所需的形状（onConnect 与 isValidConnection 共用一份）。 */
  const graphShape = useCallback(
    () => ({
      nodes: nodes.map((n) => ({
        id: n.id,
        kind: String(n.data.kind || ""),
        label: String(n.data.label || ""),
      })),
      edges: edges.map((e) => ({ source: e.source, target: e.target })),
    }),
    [nodes, edges],
  );

  // P2-3：连线先过三条规则（自环 / 成环 / 单入占用），不合法 toast 原因。
  // S2-2：同一套规则同时喂给 isValidConnection——拖拽过程中悬在非法目标上时
  // 连线实时变红（松手时 xyflow 直接丢弃该连接，onConnect 不会被调用）。
  const onConnect = useCallback(
    (c: Connection) => {
      if (!c.source || !c.target) return;
      const reason = checkConnection(graphShape(), c.source, c.target);
      if (reason) {
        toast(reason, "error");
        return;
      }
      setEdges((eds) => addEdge({ ...c, id: `e_${c.source}_${c.target}` }, eds));
    },
    [graphShape, setEdges],
  );

  const isValidConnection = useCallback(
    (c: Connection | Edge) =>
      Boolean(c.source && c.target) && checkConnection(graphShape(), c.source, c.target) === null,
    [graphShape],
  );

  // S2-7：一键整理布局。只改 position（见 autoLayout.ts 约束①），
  // useUndo 会自动记一步，整理完可以 mod+z 撤回来。
  const doAutoLayout = useCallback(() => {
    if (nodes.length === 0) {
      toast("画布还是空的", "error");
      return;
    }
    const laid = computeAutoLayout(nodes, edges);
    setNodes(laid);
    setSelectedId("");
    toast(`已整理 ${laid.length} 个节点的布局（可撤销）`, "ok");
  }, [nodes, edges, setNodes]);

  const patchNode = useCallback(
    (id: string, patch: Partial<NodeData>) => {
      setNodes((nds) => nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } } : n)));
    },
    [setNodes],
  );
  const deleteNode = useCallback(
    (id: string) => {
      setNodes((nds) => nds.filter((n) => n.id !== id));
      setEdges((eds) => eds.filter((e) => e.source !== id && e.target !== id));
      setSelectedId("");
    },
    [setNodes, setEdges],
  );

  /** 画布上的节点 → 服务端 graph（剥掉 xyflow 的运行时字段 + schema 外的字段）。 */
  const buildGraph = useCallback(() => {
    const strip = (n: Node<NodeData>) => {
      const kind = String(n.data.kind || "start") as Kind;
      // P2-4：display === false / 不属于本 kind 的字段不进提交载荷
      const cleaned = cleanData(kind, n.data as Record<string, unknown>);
      return {
        id: n.id,
        type: kind,
        position: n.position,
        data: cleaned,
      };
    };
    return {
      nodes: nodes.map(strip),
      edges: edges.map((e) => ({ id: e.id, source: e.source, target: e.target })),
    };
  }, [nodes, edges]);

  async function doValidate() {
    try {
      const d = await api<{ ok: boolean; errors: Record<string, string[]> }>("/api/workflows/validate", {
        method: "POST",
        body: { graph: buildGraph() },
      });
      setErrors(d.errors || {});
      if (d.ok) {
        toast("校验通过", "ok");
        setNodes((nds) => nds.map((n) => ({ ...n, data: { ...n.data, errors: [] } })));
      } else {
        toast("有校验错误，看节点红框", "error");
        setNodes((nds) => nds.map((n) => ({ ...n, data: { ...n.data, errors: d.errors[n.id] || (n.id === "_graph" ? undefined : []) } })));
      }
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  async function doSave() {
    const graph = buildGraph();
    try {
      if (current && !current.builtin) {
        const d = await api<{ version: string }>(`/api/workflows/${current.id}`, {
          method: "PUT",
          body: { name: wfName, graph },
        });
        toast(`已保存 v${d.version}`, "ok");
      } else {
        let name = wfName;
        openModal({
          title: "保存为新工作流",
          body: (
            <div className="field">
              <label htmlFor="wf-name">名称</label>
              <input id="wf-name" defaultValue={name} onChange={(e) => (name = e.target.value)} />
            </div>
          ),
          actions: [
            { label: "取消" },
            {
              label: "保存",
              kind: "btn-primary",
              onClick: async (close) => {
                const d = await api<{ ok: boolean; id: string }>("/api/workflows", {
                  method: "POST",
                  body: { name: name.trim() || "未命名工作流", graph },
                });
                close();
                toast("已创建", "ok");
                setCurrent({ id: d.id, name, description: "", builtin: false, updated_at: "", graph });
                setWfName(name);
                await paintList();
              },
            },
          ],
        });
      }
      setErrors({});
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  function startStream(path: string, body: Record<string, unknown>, resetStatus = true) {
    setRunning(true);
    // 新一轮：状态清空，未走到的节点重新置灰。审批续跑不重置——
    // 引擎对已跑过的节点不发事件，清了会把 s1/l1 的「完成」弄丢。
    if (resetStatus) setRunStatuses(emptyRunStatus());
    setLogs([{ text: "开始运行…", kind: "log" }]);
    setOutput("");
    streamSSE(path, body, {
      onEvent(event, data) {
        if (event === "node") {
          const nodeId = String(data.node_id || "");
          const status = String(data.status || "");
          // P2-1：只写偏表，不碰节点集合
          setRunStatuses((t) => ({
            ...t,
            [nodeId]: { status: status as RunStatusTable[string]["status"], error: String(data.error || "") || undefined },
          }));
          if (status === "done" && data.output !== undefined) {
            setLogs((l) => [...l, { text: `${String(data.label || nodeId)} 完成`, kind: "done" }]);
          } else if (status === "failed") {
            setLogs((l) => [...l, { text: `${String(data.label || nodeId)} 失败：${String(data.error || "")}`, kind: "failed" }]);
          } else if (status === "running") {
            setLogs((l) => [...l, { text: `${String(data.label || nodeId)} 运行中`, kind: "running" }]);
          }
        } else if (event === "log") {
          setLogs((l) => [...l, { text: String(data.message || ""), kind: "log" }]);
        } else if (event === "artifact") {
          setLogs((l) => [...l, { text: `制品：${String(data.title || data.artifact_id || "")}`, kind: "artifact" }]);
        } else if (event === "approval") {
          const nodeId = String(data.node_id || "");
          setApproval({ runId: String(data.run_id || ""), nodeId, reason: String(data.reason || "") });
          // 引擎对审批节点不发 node 事件，这里手动标「待确认」
          setRunStatuses((t) => ({ ...t, [nodeId]: { status: "waiting" } }));
          setLogs((l) => [...l, { text: `等待审批：${String(data.reason || "")}`, kind: "waiting" }]);
        } else if (event === "done") {
          setLogs((l) => [...l, { text: "运行结束", kind: "done" }]);
          if (data.output && typeof data.output === "object") {
            const out = data.output as Record<string, unknown>;
            const lastKey = Object.keys(out).pop();
            if (lastKey) setOutput(String(out[lastKey] ?? ""));
          }
          toast("工作流跑完了", "ok");
        } else if (event === "error") {
          setLogs((l) => [...l, { text: String(data.message || "执行失败"), kind: "failed" }]);
          toast(String(data.message || "执行失败"), "error");
        }
      },
      onError(message) {
        setLogs((l) => [...l, { text: message, kind: "failed" }]);
      },
      onEnd() {
        setRunning(false);
        void paintList();
      },
    });
  }

  async function doRun() {
    if (!current) {
      toast("先保存再运行", "error");
      return;
    }
    startStream(`/api/workflows/${current.id}/run`, { inputs: {} });
  }

  async function doResume(approved: boolean) {
    if (!approval) return;
    if (!approved) {
      setApproval(null);
      setLogs((l) => [...l, { text: "已拒绝，运行终止（该分支不再继续）", kind: "failed" }]);
      return;
    }
    const runId = approval.runId;
    const nodeId = approval.nodeId;
    setApproval(null);
    // resume 时引擎把审批节点放进 skipped 不再发事件——手动补「完成」
    setRunStatuses((t) => ({ ...t, [nodeId]: { status: "done" } }));
    startStream(`/api/workflows/runs/${runId}/resume`, { inputs: {} }, false);
  }

  function loadWorkflow(wf: WfRow) {
    setCurrent(wf);
    setWfName(wf.name);
    // 服务端 graph：{id, type: kind, position, data} —— 画布用 data.kind，补回去
    setNodes(
      (wf.graph?.nodes || []).map((n) => ({
        id: n.id,
        type: "da",
        position: n.position,
        data: { ...(n.data || {}), kind: ((n.data?.kind as string) || n.type || "start") as Kind },
      })),
    );
    setEdges((wf.graph?.edges || []).map((e) => ({ id: e.id, source: e.source, target: e.target })));
    setErrors({});
    setRunStatuses(emptyRunStatus());
    setSelectedId("");
    setLogs([]);
    refit();
  }

  function newFlow() {
    setCurrent(null);
    setWfName("未命名工作流");
    setNodes([newNode("start", [])]);
    setEdges([]);
    setErrors({});
    setRunStatuses(emptyRunStatus());
    setLogs([]);
    refit();
  }

  function showBuiltin() {
    openModal({
      title: "内置工作流模板",
      wide: true,
      body: (
        <p className="small">
          内置模板在服务端 <code>app/services/workflow/builtin.py</code> 定义，随版本发布。
          在左侧列表里点「使用」即可载入画布改造成自己的版本。
        </p>
      ),
      actions: [{ label: "关闭" }],
    });
  }

  const selected = useMemo(() => nodes.find((n) => n.id === selectedId) || null, [nodes, selectedId]);

  return (
    <div className="canvas-grid">
      <aside className="canvas-side">
        <div className="aside-head">
          <span>节点</span>
        </div>
        <div className="wf-palette" data-testid="wf-palette">
          {(Object.keys(KIND_META) as Kind[]).map((k) => (
            <button
              key={k}
              className="wf-palette-btn"
              data-testid={`wf-add-${k}`}
              onClick={() => {
                const n = newNode(k, nodes);
                setNodes((nds) => [...nds, n]);
                setSelectedId(n.id);
                refit();
              }}
            >
              <i style={{ background: KIND_META[k].color }} />
              {KIND_META[k].label}
            </button>
          ))}
        </div>

        <div className="aside-head" style={{ marginTop: 14 }}>
          <span>工作流</span>
          <span className="row" style={{ gap: 4 }}>
            <button className="btn btn-ghost btn-xs" onClick={newFlow}>
              新建
            </button>
            <button className="btn btn-ghost btn-xs" onClick={showBuiltin}>
              内置
            </button>
          </span>
        </div>
        <div className="wf-list" data-testid="wf-list">
          {workflows.map((wf) => (
            <button key={wf.id} className={`wf-item ${current?.id === wf.id ? "active" : ""}`} onClick={() => loadWorkflow(wf)}>
              <span className="truncate" style={{ fontWeight: 600, fontSize: 13 }}>
                {wf.name}
                {wf.builtin && <span className="badge" style={{ marginLeft: 4 }}>内置</span>}
              </span>
              <span className="tiny faint">{formatTime(wf.updated_at)}</span>
            </button>
          ))}
          {workflows.length === 0 && <p className="tiny faint">还没有工作流。加节点、连线、保存。</p>}
        </div>
      </aside>

      <section className="canvas-main">
        <div className="canvas-toolbar">
          <input
            className="canvas-name"
            value={wfName}
            data-testid="wf-name"
            onChange={(e) => setWfName(e.target.value)}
            placeholder="工作流名称"
          />
          <button className="btn btn-sm" data-testid="wf-validate" onClick={() => void doValidate()}>
            校验
          </button>
          <button className="btn btn-sm" data-testid="wf-save" onClick={() => void doSave()}>
            保存
          </button>
          <button
            className="btn btn-sm"
            data-testid="wf-autolayout"
            title="按连线关系重新排列节点（可撤销）"
            onClick={doAutoLayout}
          >
            整理布局
          </button>
          <button className="btn btn-primary btn-sm" data-testid="wf-run" disabled={running || !current} onClick={() => void doRun()}>
            {running ? "运行中…" : "运行"}
          </button>
          {current && !current.builtin && (
            <button
              className="btn btn-ghost btn-sm"
              onClick={async () => {
                if (!(await confirmDialog(`删除工作流「${current.name}」？`))) return;
                await api(`/api/workflows/${current.id}`, { method: "DELETE" });
                toast("已删除", "ok");
                newFlow();
                await paintList();
              }}
            >
              ×
            </button>
          )}
        </div>

        {Object.keys(errors).length > 0 && (
          <div className="wf-errors" data-testid="wf-errors">
            {Object.entries(errors).map(([id, msgs]) => (
              <div key={id}>
                <strong>{id}</strong>：{msgs.join("；")}
              </div>
            ))}
          </div>
        )}

        <div className="canvas-flow" data-testid="canvas-flow">
          {/* P2-5：画布空时盖一层二段式空状态（不含输入框），节点一有就消失 */}
          {nodes.length === 0 && (
            <EmptyState
              phase={workflows.length === 0 ? "never" : "empty-selection"}
              count={workflows.length}
              onOpenList={() => {
                if (workflows.length === 0) newFlow();
                else document.querySelector(".wf-item")?.scrollIntoView({ block: "center" });
              }}
            />
          )}
          {/* 画布崩了不要带走整个页面：错误边界内部消化，画布区单独降级 */}
          <ErrorBoundary
            label="画布"
            onCaughtError={(err, info) => reportClientError(err, String(info.componentStack || ""), "canvas-flow")}
          >
            <ReactFlow
              nodes={displayNodes}
              edges={edges}
              nodeTypes={nodeTypes}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              isValidConnection={isValidConnection}
              connectionLineComponent={DaConnectionLine}
              snapGrid={[18, 18]}
              onNodeClick={(_, n) => setSelectedId(n.id)}
              onPaneClick={() => setSelectedId("")}
              fitView
              proOptions={{ hideAttribution: true }}
            >
              <Background gap={18} />
              <MiniMap pannable zoomable />
              <Controls />
            </ReactFlow>
          </ErrorBoundary>
        </div>

        {(logs.length > 0 || approval) && (
          <div className="canvas-bottom" data-testid="wf-timeline">
            {approval && (
              <div className="approval-card" style={{ marginBottom: 10 }}>
                <div className="msg-label">工作流等待审批</div>
                <div className="approval-title">{approval.reason || "这一步需要确认"}</div>
                <div className="approval-actions">
                  <button className="btn btn-primary btn-sm" data-testid="wf-approve" onClick={() => void doResume(true)}>
                    通过，继续
                  </button>
                  <button className="btn btn-ghost btn-sm" data-testid="wf-reject" onClick={() => void doResume(false)}>
                    拒绝
                  </button>
                </div>
              </div>
            )}
            <div className="wf-log" data-testid="wf-log">
              {logs.map((l, i) => (
                <div key={i} className={`wf-log-line ${l.kind}`}>
                  {l.text}
                </div>
              ))}
            </div>
            {output && (
              <div className="wf-output md" data-testid="wf-output" style={{ whiteSpace: "pre-wrap" }}>
                {output}
              </div>
            )}
          </div>
        )}
      </section>

      <PropPanel node={selected} meta={meta} onChange={patchNode} onDelete={deleteNode} />
    </div>
  );
}
