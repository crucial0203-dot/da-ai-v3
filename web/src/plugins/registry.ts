/**
 * UI 插件协议 v0.1 —— 对标 deepseek-harness 的插件入口约定：
 *   manifest 声明身份与依赖，activate(ctx) 拿到注册能力，返回 disposer 即可逆。
 *
 * v0.1 只加载第一方构建产物（编译进 bundle 的内置插件）。
 * 运行时动态加载第三方插件是 v0.3 的事——安全门槛没建好之前不做。
 */
import * as React from "react";
import { registerSlot, type SlotEntry } from "./slots";

export interface PluginManifest {
  id: string;
  name: string;
  version: string;
  description?: string;
  /** 声明要贡献的槽位（写进设置页展示，也作自查） */
  ui?: { slots: string[] };
  /** 声明后端能力（工具/路由），由后端同 id 插件提供 */
  backend?: { tools?: string[] };
}

export interface PluginContext {
  slots: {
    /** 向槽位注入组件，返回 disposer */
    register(slot: string, entry: Omit<SlotEntry, "pluginId">): () => void;
  };
  /** 提示条（复用全局 toast） */
  toast(msg: string, kind?: "info" | "error"): void;
  /** 动态模块没有构建期 React，由宿主注入 */
  React: typeof React;
  h: typeof React.createElement;
}

export interface UIPlugin {
  manifest: PluginManifest;
  activate(ctx: PluginContext): void | (() => void);
}

const active = new Map<string, { manifest: PluginManifest; dispose: () => void }>();

export function activatePlugin(plugin: UIPlugin): boolean {
  if (active.has(plugin.manifest.id)) return true;
  const disposers: Array<() => void> = [];
  const ctx: PluginContext = {
    slots: {
      register: (slot, entry) => {
        const off = registerSlot(slot, { ...entry, pluginId: plugin.manifest.id });
        disposers.push(off);
        return off;
      },
    },
    toast: (msg, kind) => {
      window.dispatchEvent(new CustomEvent("da:toast", { detail: { msg, kind: kind ?? "info" } }));
    },
    React,
    h: React.createElement,
  };
  try {
    const maybe = plugin.activate(ctx);
    if (typeof maybe === "function") disposers.push(maybe);
  } catch (err) {
    console.error(`[plugins] 插件 ${plugin.manifest.id} 激活失败:`, err);
    disposers.forEach((d) => d());
    return false;
  }
  active.set(plugin.manifest.id, {
    manifest: plugin.manifest,
    dispose: () => disposers.reverse().forEach((d) => d()),
  });
  return true;
}

export function deactivatePlugin(id: string): void {
  const rec = active.get(id);
  if (!rec) return;
  rec.dispose();
  active.delete(id);
}

export function listActivePlugins(): PluginManifest[] {
  return [...active.values()].map((r) => r.manifest);
}

/** 批量激活（入口） */
export function activateAll(plugins: UIPlugin[]): void {
  for (const p of plugins) activatePlugin(p);
}
