/**
 * 全局状态（zustand）：会话（meta/me/tasks）、主题、toast、modal。
 */
import { create } from "zustand";
import { ApiError, api } from "../lib/api";

export interface ModelSpec {
  id: string;
  label: string;
  short: string;
  tier: string;
  vision: boolean;
  provider: string;
  note: string;
  available: boolean;
  default?: boolean;
}

export interface ModeSpec {
  key: string;
  label: string;
  desc: string;
}

export interface UploadRules {
  ext: string[];
  image_ext: string[];
  max_mb: number;
  max_image_mb: number;
  max_files_per_batch: number;
  max_images_per_task: number;
}

export interface WebProvider {
  id: string;
  label: string;
  available: boolean;
  degraded: boolean;
  note: string;
}

export interface WebMeta {
  enabled: boolean;
  top_k: number;
  providers: WebProvider[];
}

export interface RagMeta {
  enabled: boolean;
  fts5: boolean;
  top_k: number;
}

export interface Meta {
  app: string;
  model: string;
  models: ModelSpec[];
  default_model: string;
  efforts: string[];
  effort_labels: Record<string, string>;
  upload: UploadRules;
  modes: ModeSpec[];
  web?: WebMeta;
  rag?: RagMeta;
  [k: string]: unknown;
}

export interface Me {
  id: string;
  nickname: string;
  role: string;
  pref_model: string;
  pref_effort: string;
  pref_theme: string;
  pref_keep_thinking: boolean;
  quota?: { used: number; quota: number };
  [k: string]: unknown;
}

export interface TaskSummary {
  id: string;
  title: string;
  mode: string;
  status: string;
  goal: string;
  nodes_done: number;
  nodes_total: number;
  nodes_failed: number;
  updated_at: string;
  [k: string]: unknown;
}

/* ── 主题 ─────────────────────────────────────────────── */
export type Theme = "light" | "dark" | "oled";

function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem("da_theme");
    if (saved === "light" || saved === "dark" || saved === "oled") return saved;
  } catch {
    /* ignore */
  }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/* ── toast ────────────────────────────────────────────── */
interface ToastItem {
  id: number;
  text: string;
  kind: "info" | "error" | "ok";
}
interface ToastState {
  items: ToastItem[];
  push: (text: string, kind?: ToastItem["kind"], ms?: number) => void;
  remove: (id: number) => void;
}
export const useToasts = create<ToastState>((set) => ({
  items: [],
  push: (text, kind = "info", ms = 2600) => {
    const id = Date.now() + Math.random();
    set((s) => ({ items: [...s.items, { id, text, kind }] }));
    setTimeout(() => set((s) => ({ items: s.items.filter((t) => t.id !== id) })), ms);
  },
  remove: (id) => set((s) => ({ items: s.items.filter((t) => t.id !== id) })),
}));

export function toast(text: string, kind: ToastItem["kind"] = "info", ms?: number) {
  useToasts.getState().push(text, kind, ms);
}

/* ── modal / confirm ──────────────────────────────────── */
export interface ModalSpec {
  title: React.ReactNode;
  body: React.ReactNode;
  wide?: boolean;
  /** 全屏级弹窗（制品预览等）：宽度接近视口，高度吃掉剩余空间 */
  full?: boolean;
  actions?: Array<{ label: string; kind?: string; onClick?: (close: () => void) => void }>;
}
interface ModalState {
  specs: ModalSpec[];
  open: (spec: ModalSpec) => void;
  close: () => void;
}
export const useModals = create<ModalState>((set) => ({
  specs: [],
  open: (spec) => set((s) => ({ specs: [...s.specs, spec] })),
  close: () => set((s) => ({ specs: s.specs.slice(0, -1) })),
}));

export function openModal(spec: ModalSpec) {
  useModals.getState().open(spec);
}

export function confirmDialog(message: string, opts: { danger?: boolean; okLabel?: string } = {}): Promise<boolean> {
  return new Promise((resolve) => {
    useModals.getState().open({
      title: "确认",
      body: <p style={{ margin: 0 }}>{message}</p>,
      actions: [
        { label: "取消", onClick: (close) => { close(); resolve(false); } },
        {
          label: opts.okLabel || "确定",
          kind: opts.danger ? "btn-danger" : "btn-primary",
          onClick: (close) => { close(); resolve(true); },
        },
      ],
    });
  });
}

/* ── 会话 ─────────────────────────────────────────────── */
interface SessionState {
  meta: Meta | null;
  me: Me | null;
  tasks: TaskSummary[];
  theme: Theme;
  booted: boolean;
  authRequired: boolean;
  /** 今日额度（额度条数据源）。每轮对话/审批后要重新拉，否则一直停在登录时的数字 */
  quota: { used: number; quota: number } | null;
  /** 全局命令面板（mod+k）开关 */
  paletteOpen: boolean;
  setPaletteOpen: (v: boolean) => void;
  setTheme: (t: Theme) => void;
  boot: () => Promise<void>;
  refreshTasks: () => Promise<void>;
  refreshUsage: () => Promise<void>;
  reloadMe: () => Promise<void>;
  logout: () => Promise<void>;
}

function applyTheme(t: Theme) {
  document.documentElement.dataset.theme = t;
  try {
    localStorage.setItem("da_theme", t);
  } catch {
    /* ignore */
  }
}

export const useSession = create<SessionState>((set, get) => ({
  meta: null,
  me: null,
  tasks: [],
  theme: initialTheme(),
  booted: false,
  authRequired: false,
  quota: null,
  paletteOpen: false,
  setPaletteOpen: (v) => set({ paletteOpen: v }),

  setTheme: (t) => {
    applyTheme(t);
    set({ theme: t });
    const { me } = get();
    if (me && me.pref_theme !== t) {
      set({ me: { ...me, pref_theme: t } });
      void api("/api/me", { method: "PATCH", body: { pref_theme: t } }).catch(() => undefined);
    }
  },

  boot: async () => {
    applyTheme(get().theme);
    try {
      const meta = await api<Meta>("/api/meta");
      const me = await api<Me>("/api/me");
      if (me.pref_theme === "light" || me.pref_theme === "dark" || me.pref_theme === "oled") {
        applyTheme(me.pref_theme as Theme);
        set({ theme: me.pref_theme as Theme });
      }
      set({ meta, me, booted: true, authRequired: false, quota: me.quota ?? null });
      // 刷新任务列表不阻塞 boot——否则 onDone 的导航会迟到，
      // 把用户在登录完成瞬间点开的页面拽回首页（2026-02 线上插件页竞态 bug）
      void get().refreshTasks();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        set({ authRequired: true, booted: true });
        return;
      }
      throw err;
    }
  },

  refreshTasks: async () => {
    const { me } = get();
    if (me?.role && me.role !== "visitor") {
      set({ tasks: [] });
      return;
    }
    try {
      // 100 = 后端上限。侧栏首屏 50、滚到底再 50（AppShell 的增量渲染），
      // 取满一屏半才滚得动；更早的会话靠搜索（后端 offset 分页留给以后）。
      const data = await api<{ tasks: TaskSummary[] }>("/api/tasks?limit=100");
      set({ tasks: data.tasks });
    } catch {
      set({ tasks: [] });
    }
  },

  refreshUsage: async () => {
    const { me } = get();
    // 管理员没有每日额度；别为了一条不显示的横幅白打一次接口
    if (!me || me.role !== "visitor") {
      set({ quota: me?.quota ?? null });
      return;
    }
    try {
      const data = await api<{ quota: { used: number; quota: number } }>("/api/usage");
      set({ quota: data.quota });
    } catch {
      // 额度条是辅助信息，刷新失败不打断主流程，保留上一次的数字
    }
  },

  reloadMe: async () => {
    const me = await api<Me>("/api/me");
    set({ me, quota: me.quota ?? get().quota });
  },

  logout: async () => {
    await api("/api/auth/logout", { method: "POST" }).catch(() => undefined);
    set({ me: null, tasks: [], authRequired: true, quota: null });
  },
}));

/* ── 模型辅助（读 meta）──────────────────────────────── */
export function modelsOf(meta: Meta | null): ModelSpec[] {
  return meta?.models ?? [];
}
export function defaultModelId(meta: Meta | null): string {
  return meta?.default_model || meta?.models?.[0]?.id || "";
}
export function getModel(meta: Meta | null, id: string): ModelSpec | undefined {
  return meta?.models.find((m) => m.id === id);
}
export function effortLabel(meta: Meta | null, key: string): string {
  if (!meta) return key;
  if (!key) return "";
  return meta.effort_labels?.[key] || key;
}

/** 跨视图的一次性派发（首页发消息 → 任务页自动发出）。 */
export interface PendingDispatch {
  taskId: string;
  text: string;
  model: string;
  effort: string;
  planMode: boolean;
  skillIds: string[];
  webSearch?: boolean;
  ragIds?: string[];
  toolNames?: string[];
}
export function setPendingDispatch(d: PendingDispatch) {
  try {
    sessionStorage.setItem("da_pending_dispatch", JSON.stringify(d));
  } catch {
    /* ignore */
  }
}
export function takePendingDispatch(taskId: string): PendingDispatch | null {
  try {
    const raw = sessionStorage.getItem("da_pending_dispatch");
    if (!raw) return null;
    const d = JSON.parse(raw) as PendingDispatch;
    if (d.taskId !== taskId) return null;
    sessionStorage.removeItem("da_pending_dispatch");
    return d;
  } catch {
    return null;
  }
}

/* ── 客户端错误上报 ─────────────────────────────────────
 * React 错误边界捕获的异常不会冒泡到 window.onerror，不主动上报
 * 线上就是「崩了但没人知道」。这里统一 POST 到 /api/client-error。
 * 两条纪律：上报失败必须静默（不能二次抛错）；同指纹 30 秒内去重
 * （渲染崩溃会随每次重渲染重复触发，不去重会刷爆审计表）。
 */
const _errDedupe = new Map<string, number>();

export function reportClientError(err: Error, stack: string, label = "") {
  try {
    const fingerprint = `${label}|${err.name}|${err.message}`.slice(0, 300);
    const now = Date.now();
    const last = _errDedupe.get(fingerprint) || 0;
    if (now - last < 30_000) return;
    _errDedupe.set(fingerprint, now);
    if (_errDedupe.size > 100) {
      for (const [k, t] of _errDedupe) if (now - t > 60_000) _errDedupe.delete(k);
    }
    void fetch("/api/client-error", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        label,
        message: String(err.message || err).slice(0, 2000),
        stack: (stack || err.stack || "").slice(0, 2000),
        path: location.pathname,
        ts: new Date().toISOString(),
      }),
    }).catch(() => {
      /* 上报失败不能二次抛错 */
    });
  } catch {
    /* ignore */
  }
}
