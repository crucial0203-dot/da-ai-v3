/** 内置插件清单：启动时一次性激活；停用状态持久化到 localStorage。 */
import { activateAll, deactivatePlugin, listActivePlugins, type UIPlugin } from "../registry";
import { quickActions } from "./QuickActions";
import { chatStats } from "./ChatStats";
import { pluginPanel } from "./PluginPanel";
import { artifactTurnCard } from "./ArtifactTurnCard";

export const BUILTIN_PLUGINS: UIPlugin[] = [quickActions, chatStats, pluginPanel, artifactTurnCard];

const DISABLED_KEY = "da_plugins_disabled";

function readDisabled(): Set<string> {
  try {
    const raw = JSON.parse(localStorage.getItem(DISABLED_KEY) || "[]");
    return new Set(Array.isArray(raw) ? raw.filter((x) => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

function writeDisabled(ids: Set<string>): void {
  try {
    localStorage.setItem(DISABLED_KEY, JSON.stringify([...ids]));
  } catch {
    /* 隐私模式等场景写不了就只活在本次会话 */
  }
}

export function isPluginEnabled(id: string): boolean {
  return !readDisabled().has(id);
}

/**
 * 启用/停用一个内置插件（立即生效 + 持久化）。
 * 停用走 disposer 回滚所有槽位注册，UI 随槽位订阅即时更新。
 */
export function setPluginEnabled(id: string, enabled: boolean): void {
  const disabled = readDisabled();
  if (enabled) {
    disabled.delete(id);
    const plugin = BUILTIN_PLUGINS.find((p) => p.manifest.id === id);
    if (plugin) activateAll([plugin]);
  } else {
    disabled.add(id);
    deactivatePlugin(id);
  }
  writeDisabled(disabled);
}

export function activateBuiltinPlugins(): void {
  const disabled = readDisabled();
  activateAll(BUILTIN_PLUGINS.filter((p) => !disabled.has(p.manifest.id)));
}

/** 插件页展示用：每行 = 一个内置插件 + 当前激活状态 */
export function pluginStates(): Array<{ plugin: UIPlugin; active: boolean }> {
  const activeIds = new Set(listActivePlugins().map((m) => m.id));
  return BUILTIN_PLUGINS.map((plugin) => ({
    plugin,
    active: activeIds.has(plugin.manifest.id),
  }));
}
