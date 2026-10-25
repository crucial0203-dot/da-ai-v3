/**
 * 主题插件引擎 —— 移植 dsh-market 的 ThemeDef tokens 换肤：
 * 插件携带 CSS 变量集，启用即覆写到 <html>，互斥激活（同时只有一个主题生效），
 * 停用/卸载恢复基础主题（tokens.css 的 data-theme 变量）。
 */

export interface ThemePayload {
  colorScheme?: "light" | "dark";
  tokens: Record<string, string>;
}

const APPLIED_KEY = "da_market_theme";

/**
 * 每个主题写了哪些变量，按 id 记一笔。
 *
 * 原先 clearThemeTokens() 清空 <html> 上**所有**内联 `--*`，而"主题互斥"
 * 的实现是给其余每个主题条调用一次 clear——第一个 clear 就把刚生效的新主题
 * 抹平了。变量的所有权必须落到具体 id 上：停用时只撤自己写的那些。
 */
const appliedKeys = new Map<string, string[]>();

export function applyThemeTokens(id: string, payload: ThemePayload): void {
  const root = document.documentElement;
  clearThemeTokens(id);
  const keys: string[] = [];
  for (const [k, v] of Object.entries(payload.tokens || {})) {
    if (k.startsWith("--")) {
      root.style.setProperty(k, v);
      keys.push(k);
    }
  }
  appliedKeys.set(id, keys);
  if (payload.colorScheme) root.style.colorScheme = payload.colorScheme;
  root.setAttribute("data-market-theme", id);
  try {
    localStorage.setItem(APPLIED_KEY, id);
  } catch {
    /* ignore */
  }
}

export function clearThemeTokens(id?: string): void {
  const root = document.documentElement;
  if (!id) {
    // 无参才是"全部下架"（卸载/停用所有主题时用）
    for (const name of Array.from(root.style)) {
      if (name.startsWith("--")) root.style.removeProperty(name);
    }
    root.style.colorScheme = "";
    root.removeAttribute("data-market-theme");
    appliedKeys.clear();
    try {
      localStorage.removeItem(APPLIED_KEY);
    } catch {
      /* ignore */
    }
    return;
  }

  // 别人的变量先留下：这块不再"一键全清"，只撤本次 id 写的那些。
  // 同一个变量若还被别的主题登记着，也不动它（谁写的谁负责撤）。
  const mine = appliedKeys.get(id) || [];
  for (const key of mine) {
    const claimedByOthers = Array.from(appliedKeys.entries()).some(
      ([other, keys]) => other !== id && keys.includes(key),
    );
    if (!claimedByOthers) root.style.removeProperty(key);
  }
  appliedKeys.delete(id);
  if (!appliedKeys.size) {
    root.style.colorScheme = "";
    root.removeAttribute("data-market-theme");
    try {
      localStorage.removeItem(APPLIED_KEY);
    } catch {
      /* ignore */
    }
  }
}

export function appliedThemeId(): string | null {
  try {
    return localStorage.getItem(APPLIED_KEY);
  } catch {
    return null;
  }
}
