/**
 * 插件市场 store —— 移植 dsh-market 的「目录 + 安装 + 启停」前端半区：
 * 安装/启停调 /api/market/*，成功后本地即时生效（主题换肤 / UI 模块动态加载），
 * 失败保持原状并提示。收藏存 localStorage（对标 dsh 的 market state）。
 */
import { create } from "zustand";
import { api, ApiError } from "../lib/api";
import { applyThemeTokens, clearThemeTokens, type ThemePayload } from "./theme";
import { loadUIModule, unloadUIModule } from "./runtime";

export type PluginKind = "theme" | "ui-module" | "tool-pack" | "dsh-ref";

export interface MarketEntry {
  id: string;
  name: string;
  owner: string;
  kind: PluginKind;
  category: string;
  version: string;
  description: { zh: string; en?: string };
  stars?: number | null;
  added: string;
  capabilities?: string[];
  capabilityRedLines?: string[];
  capabilityCheckedAt?: string | null;
  icon?: string;
  installed: boolean;
  enabled: boolean;
  installed_at?: string | null;
  /** 目录浏览的轻量预览 */
  preview?: ThemePayload;
  tools?: string[];
  module?: string;
  /** dsh-ref（DSH 社区目录，浏览型不可安装） */
  url?: string;
  npm?: string | null;
  downloads?: number | null;
  downloadsStart?: string | null;
  downloadsEnd?: string | null;
  no_install?: boolean;
}

export interface CatalogView {
  updated?: string;
  count: number;
  categories: Record<string, { zh: string; en?: string }>;
  plugins: MarketEntry[];
  active_theme: string | null;
}

interface MarketState {
  catalog: CatalogView | null;
  loading: boolean;
  error: string;
  busy: string;
  favorites: string[];
  refresh: () => Promise<void>;
  install: (id: string) => Promise<boolean>;
  uninstall: (id: string) => Promise<boolean>;
  toggle: (id: string, enabled: boolean) => Promise<boolean>;
  undo: () => Promise<boolean>;
  toggleFav: (id: string) => void;
}

function toast(msg: string, kind: "info" | "error" | "ok" = "info") {
  window.dispatchEvent(new CustomEvent("da:toast", { detail: { msg, kind } }));
}

function loadFavs(): string[] {
  try {
    return JSON.parse(localStorage.getItem("da_market_favs") || "[]") as string[];
  } catch {
    return [];
  }
}

function saveFavs(favs: string[]) {
  try {
    localStorage.setItem("da_market_favs", JSON.stringify(favs));
  } catch {
    /* ignore */
  }
}

async function fetchSource(id: string): Promise<string | null> {
  try {
    const d = await api<{ payload?: { source?: string } }>(`/api/market/plugin/${encodeURIComponent(id)}`);
    return d.payload?.source ?? null;
  } catch {
    return null;
  }
}

/** 本地生效：按 kind 应用/回收 */
async function applyLocal(entry: MarketEntry, enabled: boolean): Promise<void> {
  if (entry.kind === "theme") {
    if (enabled && entry.preview) applyThemeTokens(entry.id, entry.preview);
    else clearThemeTokens();
    return;
  }
  if (entry.kind === "ui-module") {
    if (enabled) {
      const source = await fetchSource(entry.id);
      if (source) await loadUIModule(entry.id, source);
    } else {
      unloadUIModule(entry.id);
    }
  }
}

export const useMarket = create<MarketState>((set, get) => ({
  catalog: null,
  loading: false,
  error: "",
  busy: "",
  favorites: loadFavs(),

  refresh: async () => {
    set({ loading: true, error: "" });
    try {
      const catalog = await api<CatalogView>("/api/market/catalog");
      set({ catalog, loading: false });
    } catch (err) {
      set({ loading: false, error: err instanceof ApiError ? err.message : "市场目录加载失败" });
    }
  },

  install: async (id) => {
    set({ busy: id });
    try {
      await api("/api/market/install", { method: "POST", body: { id } });
      const entry = get().catalog?.plugins.find((p) => p.id === id);
      if (entry) await applyLocal({ ...entry, installed: true, enabled: true }, true);
      await get().refresh();
      toast(`已安装「${entry?.name ?? id}」`, "ok");
      set({ busy: "" });
      return true;
    } catch (err) {
      toast(err instanceof ApiError ? err.message : "安装失败", "error");
      set({ busy: "" });
      return false;
    }
  },

  uninstall: async (id) => {
    set({ busy: id });
    try {
      await api("/api/market/uninstall", { method: "POST", body: { id } });
      const entry = get().catalog?.plugins.find((p) => p.id === id);
      if (entry) await applyLocal(entry, false);
      await get().refresh();
      toast(`已卸载「${entry?.name ?? id}」`, "ok");
      set({ busy: "" });
      return true;
    } catch (err) {
      toast(err instanceof ApiError ? err.message : "卸载失败", "error");
      set({ busy: "" });
      return false;
    }
  },

  toggle: async (id, enabled) => {
    set({ busy: id });
    try {
      await api("/api/market/toggle", { method: "POST", body: { id, enabled } });
      const entry = get().catalog?.plugins.find((p) => p.id === id);
      if (entry) await applyLocal(entry, enabled);
      // 主题互斥：启用一个主题后，其余主题的本地样式以服务端状态为准刷新
      if (entry?.kind === "theme" && enabled) {
        const catalog = get().catalog;
        for (const other of catalog?.plugins ?? []) {
          if (other.kind === "theme" && other.id !== id) await applyLocal(other, false).catch(() => undefined);
        }
      }
      await get().refresh();
      set({ busy: "" });
      return true;
    } catch (err) {
      toast(err instanceof ApiError ? err.message : "操作失败", "error");
      set({ busy: "" });
      return false;
    }
  },

  undo: async () => {
    try {
      const d = await api<{ message: string }>("/api/market/undo", { method: "POST", body: {} });
      // 撤销后按服务端状态全量对齐本地（简单可靠）
      await get().refresh();
      const catalog = get().catalog;
      for (const entry of catalog?.plugins ?? []) {
        await applyLocal(entry, entry.installed && entry.enabled).catch(() => undefined);
      }
      toast(d.message || "已撤销", "ok");
      return true;
    } catch (err) {
      toast(err instanceof ApiError ? err.message : "没有可撤销的操作", "error");
      return false;
    }
  },

  toggleFav: (id) => {
    const favs = get().favorites;
    const next = favs.includes(id) ? favs.filter((f) => f !== id) : [...favs, id];
    saveFavs(next);
    set({ favorites: next });
  },
}));

/** 启动时恢复：主题换肤 + UI 模块加载（工具包在服务端已恢复） */
export async function initMarketRuntime(): Promise<void> {
  const store = useMarket.getState();
  await store.refresh();
  const catalog = store.catalog;
  if (!catalog) return;
  for (const entry of catalog.plugins) {
    if (entry.installed && entry.enabled) {
      await applyLocal(entry, true).catch(() => undefined);
    }
  }
}
