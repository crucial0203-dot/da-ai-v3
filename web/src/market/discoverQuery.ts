/**
 * 市场目录的查询语义（P2-6，移植 dsh-market）：
 * - withText：设置搜索词。有词时自动切「相关性」口径——分类筛选失效
 *   （用户搜「合同」时不想被「主题」分类挡住结果）；
 * - declaredQuery：切 Tab 时清洗失效筛选（离开 discover 后 category 已无意义）；
 * - filterPlugins：按 Tab + 文本统一过滤。
 */

export type TabKey = "discover" | "themes" | "tools" | "dsh" | "favorites" | "installed" | "blocked";

export interface MarketQuery {
  tab: TabKey;
  text: string;
  /** discover / dsh 的分类筛选；其它 Tab 无效 */
  category: string;
}

export interface QueryPlugin {
  id: string;
  kind: string;
  category?: string;
  installed: boolean;
  enabled: boolean;
  name: string;
  description?: { zh?: string; en?: string };
  owner: string;
  npm?: string | null;
}

export function initialQuery(): MarketQuery {
  return { tab: "discover", text: "", category: "all" };
}

/** 输入搜索词：文本优先于分类（relevance 口径）。 */
export function withText(query: MarketQuery, text: string): MarketQuery {
  return { ...query, text };
}

/** 切 Tab：清洗失效筛选（category 只在 discover / dsh 有意义；跨族切换重置）。 */
export function declaredQuery(query: MarketQuery, tab: TabKey): MarketQuery {
  const catMatters = tab === "discover" || tab === "dsh";
  const crossFamily =
    catMatters &&
    (query.tab === "discover" || query.tab === "dsh") &&
    query.tab !== tab;
  return { ...query, tab, category: !catMatters || crossFamily ? "all" : query.category };
}

export function filterPlugins<T extends QueryPlugin>(
  plugins: T[],
  query: MarketQuery,
  favorites: string[],
): T[] {
  let list = plugins;
  switch (query.tab) {
    case "themes":
      list = list.filter((p) => p.kind === "theme");
      break;
    case "tools":
      list = list.filter((p) => p.kind === "tool-pack");
      break;
    case "dsh":
      list = list.filter((p) => p.kind === "dsh-ref");
      break;
    case "favorites":
      list = list.filter((p) => favorites.includes(p.id));
      break;
    case "installed":
      list = list.filter((p) => p.installed);
      break;
    case "blocked":
      list = list.filter((p) => p.installed && !p.enabled);
      break;
    default:
      // discover / dsh：分类过滤只在「没有搜索词」时生效（relevance 口径）
      if (
        !query.text.trim() &&
        query.category !== "all" &&
        (query.tab === "discover" || query.tab === "dsh")
      ) {
        list = list.filter((p) => p.category === query.category);
      }
  }
  // 目录条目可能来自第三方源（DSH 快照），字段缺失时不能整页崩——
  // description 已经做了兜底，剩下这几个必填字段同样按空串处理。
  const safe = (v: unknown) => String(v ?? "");
  const kw = query.text.trim().toLowerCase();
  if (kw) {
    list = list.filter(
      (p) =>
        safe(p.name).toLowerCase().includes(kw) ||
        safe(p.id).toLowerCase().includes(kw) ||
        safe(p.description?.zh).toLowerCase().includes(kw) ||
        safe(p.owner).toLowerCase().includes(kw),
    );
  }
  return list;
}
