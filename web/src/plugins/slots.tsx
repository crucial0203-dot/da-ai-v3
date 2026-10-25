/**
 * UI 槽位系统 v0.1 —— 借鉴 deepseek-harness 的 ui-slots 设计：
 * 插件把 React 组件注册进「声明过的挂载点」，宿主用 <Slot> 渲染，插件之间零 import。
 *
 * 三条纪律（照抄 dsh slots.md）：
 *   1. 声明 = 渲染授权：SLOTS 里没有的槽位拒绝注册（宿主没渲染它，注册即浪费）；
 *   2. list 是增量点（唯一 id + order 排序），single 是替换点（priority 高者胜）；
 *   3. 一切注册可逆：registerSlot 返回 disposer，插件卸载即回滚。
 */
import { useSyncExternalStore, type ComponentType } from "react";

export type SlotCardinality = "list" | "single" | "keyed";

export interface SlotDecl {
  cardinality: SlotCardinality;
  desc: string;
}

/** 槽位声明表 = 宿主授权的挂载点全集（文档即类型） */
export const SLOTS: Record<string, SlotDecl> = {
  "home.afterComposer": { cardinality: "list", desc: "首页输入框下方插件区" },
  "chat.header.actions": { cardinality: "list", desc: "会话头部操作按钮" },
  "chat.turn.tail": { cardinality: "list", desc: "对话流末尾卡片" },
  "sidebar.footer": { cardinality: "list", desc: "侧栏底部面板" },
  "settings.plugins": { cardinality: "list", desc: "设置页插件展示区" },
};

export interface SlotEntry {
  id: string;
  component: ComponentType<Record<string, unknown>>;
  pluginId: string;
  order?: number;
  priority?: number;
  key?: string;
}

const registry = new Map<string, SlotEntry[]>();
const listeners = new Set<() => void>();
let snapshot = 0; // 变更计数，驱动 useSyncExternalStore

function emit() {
  snapshot += 1;
  for (const fn of listeners) fn();
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function registerSlot(slot: string, entry: SlotEntry): () => void {
  const decl = SLOTS[slot];
  if (!decl) {
    console.warn(`[plugins] 槽位 ${slot} 未被宿主声明，拒绝注册（插件 ${entry.pluginId}）`);
    return () => {};
  }
  const list = registry.get(slot) ?? [];
  if (list.some((e) => e.id === entry.id && e.pluginId === entry.pluginId)) {
    console.warn(`[plugins] ${slot} 上重复注册 ${entry.id}，已忽略`);
    return () => {};
  }
  const next = [...list, entry];
  if (decl.cardinality === "single") {
    next.sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0));
    registry.set(slot, next.slice(0, 1)); // single 只留优胜者
  } else {
    next.sort((a, b) => (a.order ?? 100) - (b.order ?? 100));
    registry.set(slot, next);
  }
  emit();
  return () => {
    const cur = registry.get(slot) ?? [];
    registry.set(
      slot,
      cur.filter((e) => !(e.id === entry.id && e.pluginId === entry.pluginId)),
    );
    emit();
  };
}

function readEntries(slot: string): SlotEntry[] {
  return registry.get(slot) ?? [];
}

/** 插件组件订阅本槽位条目（供设置页/调试用） */
export function useSlotEntries(slot: string): SlotEntry[] {
  const stable = useSyncExternalStore(subscribe, () => snapshot, () => snapshot);
  void stable;
  return readEntries(slot);
}

/** 宿主挂载点：把 ownerProps 透传给每个插件组件 */
export function Slot({
  name,
  ...ownerProps
}: { name: string } & Record<string, unknown>) {
  const entries = useSlotEntries(name);
  const decl = SLOTS[name];
  if (!decl) return null;
  if (decl.cardinality === "keyed") {
    const wanted = String(ownerProps.slotKey ?? "");
    const hit = entries.find((e) => e.key === wanted);
    return hit ? <hit.component {...ownerProps} /> : null;
  }
  return (
    <>
      {entries.map((e) => (
        <e.component key={`${e.pluginId}:${e.id}`} {...ownerProps} />
      ))}
    </>
  );
}
