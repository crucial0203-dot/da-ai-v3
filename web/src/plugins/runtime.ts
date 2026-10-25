/**
 * UI 模块运行时 —— 把市场安装的插件源码动态加载进宿主：
 * 源码是标准 ESM（export default { manifest, activate(ctx) }），
 * 通过 Blob URL import() 加载，activate 拿到与内置插件相同的 ctx，
 * 注册进槽位、返回 disposer——停用即回滚，与 deepseek-harness 的可逆注册同构。
 */
import { activatePlugin, deactivatePlugin, type PluginContext, type UIPlugin } from "./registry";

const loaded = new Map<string, { blobUrl: string }>();

export async function loadUIModule(id: string, source: string): Promise<boolean> {
  if (loaded.has(id)) return true;
  const blobUrl = URL.createObjectURL(new Blob([source], { type: "text/javascript" }));
  try {
    const mod = (await import(/* @vite-ignore */ blobUrl)) as { default?: UIPlugin };
    const plugin = mod.default;
    if (!plugin || typeof plugin.activate !== "function") {
      console.error(`[market] 模块 ${id} 没有导出 default { manifest, activate }`);
      URL.revokeObjectURL(blobUrl);
      return false;
    }
    // 身份以安装记录为准，不信任模块自带 manifest 的 id
    const ok = activatePlugin({ ...plugin, manifest: { ...plugin.manifest, id } });
    if (!ok) {
      URL.revokeObjectURL(blobUrl);
      return false;
    }
    loaded.set(id, { blobUrl });
    return true;
  } catch (err) {
    console.error(`[market] 模块 ${id} 加载失败:`, err);
    URL.revokeObjectURL(blobUrl);
    return false;
  }
}

export function unloadUIModule(id: string): void {
  deactivatePlugin(id);
  const rec = loaded.get(id);
  if (rec) {
    URL.revokeObjectURL(rec.blobUrl);
    loaded.delete(id);
  }
}

export function isModuleLoaded(id: string): boolean {
  return loaded.has(id);
}

export type { PluginContext };
