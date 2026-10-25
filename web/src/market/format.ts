/**
 * 市场纯函数层 —— 移植 dsh-market 的 market-data.ts：
 * 计数格式化 / 头像色块 / 能力分组 / 搜索相关性 / 排序 / 分类前置。
 * 全部无副作用，可独立单测；排序口径：
 *   - 无下载量的条目排在有数之后、彼此按星排（覆盖缺口不是 0 下载）；
 *   - 有搜索词时相关性优先（pluginRelevance），所选排序只作 tie-break。
 */

export type SortField = "downloads" | "stars" | "added";
export interface SortSpec {
  field: SortField;
  dir: "asc" | "desc";
}

export const DEFAULT_SORT: SortSpec = { field: "downloads", dir: "desc" };

const SORT_KEY = "da_market_sort";

export function loadSort(): SortSpec {
  try {
    const raw = JSON.parse(localStorage.getItem(SORT_KEY) || "null") as SortSpec | null;
    if (raw && ["downloads", "stars", "added"].includes(raw.field) && ["asc", "desc"].includes(raw.dir)) {
      return raw;
    }
  } catch {
    /* ignore */
  }
  return { ...DEFAULT_SORT };
}

export function saveSort(sort: SortSpec): void {
  try {
    localStorage.setItem(SORT_KEY, JSON.stringify(sort));
  } catch {
    /* ignore */
  }
}

/** ↓11.9k / <1000 显示精确值（dsh formatCount 同款）。 */
export function formatCount(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "";
  if (n < 1000) return String(n);
  if (n < 1_000_000) {
    const k = n / 1000;
    return `${k >= 100 ? Math.round(k) : k.toFixed(1).replace(/\.0$/, "")}k`;
  }
  const m = n / 1_000_000;
  return `${m >= 100 ? Math.round(m) : m.toFixed(1).replace(/\.0$/, "")}M`;
}

/** 首字母色块头像：字符串哈希到 hsl（dsh avatarColor 同款）。 */
export function avatarColor(seed: string): string {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360} 42% 46%)`;
}

export function avatarLetter(owner: string, name: string): string {
  const s = (name || owner || "?").replace(/^(dsh-|da[-_.])/, "");
  return (s[0] || "?").toUpperCase();
}

// ---- 能力披露 ----

export const CAPABILITY_GROUPS: { key: string; label: string; items: string[] }[] = [
  { key: "fs", label: "文件", items: ["fs-read", "fs-write", "env"] },
  { key: "net", label: "网络", items: ["network", "llm"] },
  { key: "exec", label: "执行", items: ["shell", "dynamic-code", "subagent"] },
  { key: "cred", label: "密钥", items: ["credentials"] },
  { key: "host", label: "宿主", items: ["host-runtime", "theme", "ui"] },
];

const CAP_LABEL: Record<string, string> = {
  "fs-read": "读文件", "fs-write": "写文件", env: "读环境变量",
  network: "访问网络", llm: "调用模型", shell: "执行命令",
  "dynamic-code": "动态代码", subagent: "子代理", credentials: "读取凭据",
  "host-runtime": "宿主运行时", theme: "换肤", ui: "改界面",
};

export function capLabel(cap: string): string {
  return CAP_LABEL[cap] || cap;
}

/** 按碰到什么分组；未知能力进「其他」不丢弃（dsh 同款）。 */
export function groupCapabilities(caps: string[]): { key: string; label: string; items: string[] }[] {
  const known = new Map<string, string>(); // cap -> groupKey
  for (const g of CAPABILITY_GROUPS) for (const item of g.items) known.set(item, g.key);
  const out = new Map<string, { key: string; label: string; items: string[] }>();
  const others: string[] = [];
  for (const cap of caps) {
    const gk = known.get(cap);
    if (!gk) {
      others.push(cap);
      continue;
    }
    const g = CAPABILITY_GROUPS.find((x) => x.key === gk)!;
    if (!out.has(gk)) out.set(gk, { key: g.key, label: g.label, items: [] });
    out.get(gk)!.items.push(cap);
  }
  const groups = [...out.values()];
  if (others.length) groups.push({ key: "other", label: "其他", items: others });
  return groups;
}

// ---- 搜索 ----

/** NFKC + 中西文边界插空格：`MCP管理` 能命中 `管理 MCP`（dsh 同款）。 */
export function normalizeQuery(text: string): string {
  const nfkc = text.normalize("NFKC");
  const spaced = nfkc
    .replace(/([A-Za-z0-9])([\u4e00-\u9fff])/g, "$1 $2")
    .replace(/([\u4e00-\u9fff])([A-Za-z0-9])/g, "$1 $2");
  return spaced.toLowerCase().trim();
}

export interface RelevancePlugin {
  id: string;
  name: string;
  owner: string;
  category?: string;
  npm?: string | null;
  description?: { zh?: string; en?: string };
}

/** 单字段相关性：整等 +300 / 全 token 子串 +200 / 散布 +150 / 部分命中 0。 */
function fieldRelevance(field: string, tokens: string[]): number {
  if (!field) return 0;
  const f = normalizeQuery(field);
  if (!f) return 0;
  if (tokens.length === 1) {
    if (f === tokens[0]) return 300;
    if (f.includes(tokens[0])) return 200;
    return 0;
  }
  // 多词必须同字段全命中（dsh 规则）
  if (tokens.every((t) => f.includes(t))) return f === tokens.join(" ") ? 300 : 200;
  return 0;
}

/** 相关性：name 700 > npm 690 > owner 400 > zh 描述 280 > en 描述 240 > 分类 180 > id 150。 */
export function pluginRelevance(p: RelevancePlugin, rawQuery: string): number {
  const tokens = normalizeQuery(rawQuery).split(/\s+/).filter(Boolean);
  if (!tokens.length) return 0;
  const weights: [string, number][] = [
    [p.name || "", 700],
    [p.npm || "", 690],
    [p.owner || "", 400],
    [p.description?.zh || "", 280],
    [p.description?.en || "", 240],
    [p.category || "", 180],
    [p.id || "", 150],
  ];
  let best = 0;
  for (const [field, weight] of weights) {
    const rel = fieldRelevance(field, tokens);
    if (rel > 0) best = Math.max(best, weight + rel);
  }
  return best;
}

// ---- 排序 ----

interface SortableEntry {
  stars?: number | null;
  downloads?: number | null;
  added?: string;
  name?: string;
}

/** dsh comparePlugins 同款：无 downloads 恒排有数之后、彼此按星排。 */
export function comparePlugins(a: SortableEntry, b: SortableEntry, sort: SortSpec): number {
  const mul = sort.dir === "desc" ? -1 : 1;
  if (sort.field === "downloads") {
    const da = a.downloads ?? null;
    const db = b.downloads ?? null;
    if (da !== null && db !== null) return (da - db) * mul;
    if (da !== null) return -1; // 有数在前
    if (db !== null) return 1;
    return ((a.stars || 0) - (b.stars || 0)) * mul; // 都没有：按星
  }
  if (sort.field === "stars") {
    const sa = a.stars || 0;
    const sb = b.stars || 0;
    if (sa !== sb) return (sa - sb) * mul;
    return ((a.downloads || 0) - (b.downloads || 0)) * mul;
  }
  // added：ISO 字符串序即可
  const aa = a.added || "";
  const ab = b.added || "";
  if (aa !== ab) return aa < ab ? -mul : mul;
  return (a.name || "").localeCompare(b.name || "");
}

// ---- 分类 chips ----

/** 折叠时把选中分类前置，保证它可见（dsh orderedCategories 同款）。 */
export function orderedCategories(keys: string[], selected: string): string[] {
  if (selected === "all" || !keys.includes(selected)) return keys;
  return [selected, ...keys.filter((k) => k !== selected)];
}
