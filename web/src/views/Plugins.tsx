/**
 * 插件市场 —— 移植 dsh-market 的信息架构：
 *   发现 Discover（搜索 + 分类 + 卡片网格 + 确认安装）
 *   主题 Themes（色板预览 + 一键换肤，互斥激活）
 *   工具 Tools / DSH 社区（浏览型条目：需 DSH 宿主，只看不装）
 *   收藏 Favorites（本地书签）/ 已安装 / 停用
 * 安装只认市场目录（拒装未知源）；一切启停即时生效、可一步撤销。
 * 排序/相关性/能力披露/分类折叠移植自 dsh-market（见 web/src/market/format.ts）。
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import { SLOTS, useSlotEntries } from "../plugins/slots";
import { pluginStates, setPluginEnabled, isPluginEnabled } from "../plugins/builtin";
import { useMarket, type MarketEntry } from "../plugins/market";
import { declaredQuery, filterPlugins, initialQuery, withText, type TabKey } from "../market/discoverQuery";
import {
  avatarColor, avatarLetter, capLabel, comparePlugins, formatCount,
  groupCapabilities, loadSort, orderedCategories, pluginRelevance,
  saveSort, type SortSpec,
} from "../market/format";

interface BackendPlugin {
  id: string;
  name: string;
  version: string;
  description?: string;
  status: string;
  provides?: { tools?: string[]; services?: string[]; hooks?: string[] };
}

const KIND_LABEL: Record<string, string> = {
  theme: "主题", "ui-module": "界面", "tool-pack": "工具", "dsh-ref": "DSH 社区",
};

function SlotMapRow({ slot }: { slot: string }) {
  const entries = useSlotEntries(slot);
  const decl = SLOTS[slot];
  return (
    <div className="plugin-slot-row" data-testid="slot-row">
      <div className="row" style={{ gap: 8, alignItems: "baseline" }}>
        <code className="plugin-slot-name">{slot}</code>
        <span className="badge">{decl.cardinality === "list" ? "可叠加" : decl.cardinality === "single" ? "单实例" : "按键匹配"}</span>
        <span className="muted" style={{ fontSize: 12 }}>{decl.desc}</span>
      </div>
      <div className="row wrap" style={{ gap: 4, marginTop: 4 }}>
        {entries.length === 0 ? (
          <span className="muted" style={{ fontSize: 12 }}>（空）</span>
        ) : (
          entries.map((e) => (
            <span key={`${e.pluginId}:${e.id}`} className="badge badge-ok" title={`来自插件 ${e.pluginId}`}>
              {e.pluginId.replace("builtin.", "").replace("ui.", "")} → {e.id}
            </span>
          ))
        )}
      </div>
    </div>
  );
}

function Swatches({ tokens }: { tokens: Record<string, string> }) {
  const keys = ["--bg", "--surface", "--primary", "--accent"];
  return (
    <div className="market-swatches" aria-hidden>
      {keys.map((k) => (
        <span key={k} style={{ background: tokens[k] || "var(--line)" }} />
      ))}
    </div>
  );
}

/** 首字母色块头像（dsh OwnerAvatar 同款：icon 优先，无 icon 才用色块）。 */
function Avatar({ owner, name, icon }: { owner: string; name: string; icon?: string }) {
  if (icon) return <span className="market-avatar" style={{ background: "transparent" }} aria-hidden>{icon}</span>;
  return (
    <span className="market-avatar" style={{ background: avatarColor(owner + name) }} aria-hidden>
      {avatarLetter(owner, name)}
    </span>
  );
}

/** byline：owner · 版本 · ★ · ↓近30天（dsh 卡片信息密度）。 */
function Byline({ entry }: { entry: MarketEntry }) {
  return (
    <div className="muted market-byline" style={{ fontSize: 12, margin: "2px 0 6px" }}>
      <Avatar owner={entry.owner} name={entry.name} icon={entry.icon} />
      <span>{entry.owner}</span>
      <span>·</span>
      <span>v{entry.version}</span>
      {entry.kind === "dsh-ref" && typeof entry.downloads === "number" && (
        <>
          <span>·</span>
          <span title={entry.downloadsStart && entry.downloadsEnd ? `${entry.downloadsStart} ~ ${entry.downloadsEnd}` : undefined}>
            ↓ {formatCount(entry.downloads)} / 近30天
          </span>
        </>
      )}
      {entry.stars ? (
        <>
          <span>·</span>
          <span>★ {formatCount(entry.stars)}</span>
        </>
      ) : null}
      <span>·</span>
      <span>{KIND_LABEL[entry.kind] || entry.kind}</span>
    </div>
  );
}

/** 能力披露：undefined=未扫描、[]=已扫描未检出、其余按分组渲染（dsh 语义）。 */
function CapDisclosure({ entry, detailed }: { entry: MarketEntry; detailed?: boolean }) {
  if (entry.kind !== "dsh-ref") {
    // 可装三类：目录声明的能力原样列 chip
    const caps = entry.capabilities ?? [];
    if (!caps.length) return null;
    return (
      <div className="row wrap" style={{ gap: 4 }}>
        {caps.map((c) => (
          <span key={c} className="badge" title={capLabel(c)}>能力：{capLabel(c)}</span>
        ))}
      </div>
    );
  }
  if (entry.capabilities === undefined || entry.capabilities === null) {
    return <span className="badge market-cap-unknown">未扫描</span>;
  }
  if (entry.capabilities.length === 0) {
    return <span className="badge">已扫描未检出（这不等于安全）</span>;
  }
  const groups = groupCapabilities(entry.capabilities);
  return (
    <div className="market-caps" data-testid="cap-disclosure">
      <div className="row wrap" style={{ gap: 4 }}>
        {groups.map((g) => (
          <span key={g.key} className="badge" title={g.items.map(capLabel).join("、")}>
            {g.label}：{g.items.map(capLabel).join("、")}
          </span>
        ))}
      </div>
      {(entry.capabilityRedLines?.length || 0) > 0 && detailed && (
        <div className="market-redlines" role="note">
          {entry.capabilityRedLines!.map((r, i) => (
            <div key={i} className="market-redline">⚠ {r}</div>
          ))}
        </div>
      )}
      {detailed && entry.capabilityCheckedAt && (
        <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
          扫描于 {entry.capabilityCheckedAt.slice(0, 10)} · 静态扫描只能证明「有」，不能证明「没有」。
        </p>
      )}
    </div>
  );
}

function MarketCard({
  entry,
  fav,
  busy,
  onInstall,
  onToggle,
  onUninstall,
  onFav,
}: {
  entry: MarketEntry;
  fav: boolean;
  busy: boolean;
  onInstall: () => void;
  onToggle: (enabled: boolean) => void;
  onUninstall: () => void;
  onFav: () => void;
}) {
  const isTheme = entry.kind === "theme";
  return (
    <div className={`plugin-card market-card ${entry.installed ? "on" : ""}`} data-testid="plugin-card">
      <div className="row-between">
        <div className="row" style={{ gap: 8, alignItems: "baseline" }}>
          <strong>{entry.name}</strong>
        </div>
        <button
          className={`btn btn-sm btn-ghost market-fav ${fav ? "on" : ""}`}
          data-testid="market-fav"
          title={fav ? "取消收藏" : "收藏"}
          aria-label={fav ? `取消收藏 ${entry.name}` : `收藏 ${entry.name}`}
          onClick={onFav}
        >
          {fav ? "★" : "☆"}
        </button>
      </div>
      <Byline entry={entry} />
      <p className="muted" style={{ margin: "0 0 8px", fontSize: 13 }}>{entry.description?.zh}</p>
      {isTheme && entry.preview?.tokens && <Swatches tokens={entry.preview.tokens} />}
      {entry.kind === "tool-pack" && entry.tools?.length ? (
        <div className="row wrap" style={{ gap: 4, marginBottom: 8 }}>
          {entry.tools.map((t) => (
            <span key={t} className="badge badge-ok">{t}</span>
          ))}
        </div>
      ) : null}
      <div className="row wrap" style={{ gap: 6, marginTop: "auto" }}>
        {!entry.installed ? (
          <button className="btn btn-primary btn-sm" data-testid="market-install-btn" disabled={busy} onClick={onInstall}>
            {busy ? "安装中…" : "安装"}
          </button>
        ) : isTheme ? (
          <>
            {entry.enabled ? (
              <button className="btn btn-sm" data-testid="market-toggle-btn" disabled={busy} onClick={() => onToggle(false)}>
                当前主题 · 停用
              </button>
            ) : (
              <button className="btn btn-primary btn-sm" data-testid="market-toggle-btn" disabled={busy} onClick={() => onToggle(true)}>
                使用
              </button>
            )}
            <button className="btn btn-ghost btn-sm" data-testid="market-uninstall-btn" disabled={busy} onClick={onUninstall}>
              卸载
            </button>
          </>
        ) : (
          <>
            <button
              className={`btn btn-sm ${entry.enabled ? "" : "btn-primary"}`}
              data-testid="market-toggle-btn"
              disabled={busy}
              onClick={() => onToggle(!entry.enabled)}
            >
              {entry.enabled ? "已启用" : "已停用"}
            </button>
            <button className="btn btn-ghost btn-sm" data-testid="market-uninstall-btn" disabled={busy} onClick={onUninstall}>
              卸载
            </button>
          </>
        )}
        {entry.installed && <span className="badge badge-ok">✓ 已安装</span>}
      </div>
    </div>
  );
}

/** DSH 社区卡：浏览型——色块头像 + 热度 + 能力披露 + 外链，不可安装。 */
function DshCard({ entry, fav, onFav }: { entry: MarketEntry; fav: boolean; onFav: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="plugin-card market-card market-dsh" data-testid="dsh-card">
      <div className="row-between">
        <div className="row" style={{ gap: 8 }}>
          <Avatar owner={entry.owner} name={entry.name} />
          <strong>{entry.name}</strong>
          <span className="badge market-dsh-badge" title="该插件运行在 DeepSeek Harness（Cordis）运行时里">需 DSH 宿主</span>
        </div>
        <button
          className={`btn btn-sm btn-ghost market-fav ${fav ? "on" : ""}`}
          data-testid="market-fav"
          title={fav ? "取消收藏" : "收藏"}
          aria-label={fav ? `取消收藏 ${entry.name}` : `收藏 ${entry.name}`}
          onClick={onFav}
        >
          {fav ? "★" : "☆"}
        </button>
      </div>
      <Byline entry={entry} />
      <p className={`muted ${open ? "" : "market-dsh-desc"}`} style={{ margin: "0 0 8px", fontSize: 13 }}>
        {entry.description?.zh}
      </p>
      <CapDisclosure entry={entry} detailed={open} />
      {open && (
        <div className="row wrap" style={{ gap: 4, marginTop: 8 }}>
          <span className="badge">分类：{entry.category}</span>
          {entry.added && <span className="badge">收录 {entry.added}</span>}
        </div>
      )}
      <div className="row wrap" style={{ gap: 6, marginTop: "auto", paddingTop: 8 }}>
        {entry.url && (
          <a
            className="btn btn-primary btn-sm"
            data-testid="dsh-github-link"
            href={entry.url}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={`在 GitHub 打开 ${entry.name}`}
          >
            去 GitHub ↗
          </a>
        )}
        {entry.npm && (
          <a
            className="btn btn-ghost btn-sm"
            data-testid="dsh-npm-link"
            href={`https://www.npmjs.com/package/${entry.npm}`}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={`在 npm 打开 ${entry.npm}`}
          >
            npm ↗
          </a>
        )}
        <button className="btn btn-ghost btn-sm" data-testid="dsh-expand-btn" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "收起 ▴" : "详情 ▾"}
        </button>
      </div>
    </div>
  );
}

/** 分类 chips：两行折叠 + 展开 chevron；折叠时选中分类前置（dsh 同款）。 */
function CategoryChips({
  categories, selected, onSelect, testidPrefix,
}: {
  categories: Record<string, { zh: string; en?: string }>;
  selected: string;
  onSelect: (key: string) => void;
  testidPrefix: string;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [capacity, setCapacity] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);
  const keys = Object.keys(categories);
  const keySig = keys.join(",");

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = () => {
      const chips = Array.from(el.querySelectorAll<HTMLElement>("[data-chip]"));
      if (!chips.length) return;
      // 先放开约束再测 offsetTop（先渲染全部再测，dsh 同款）
      const prevMax = el.style.maxHeight;
      const prevOverflow = el.style.overflow;
      el.style.maxHeight = "none";
      el.style.overflow = "visible";
      const tops = chips.map((c) => c.offsetTop);
      el.style.maxHeight = prevMax;
      el.style.overflow = prevOverflow;
      const rows = [...new Set(tops)].sort((a, b) => a - b);
      const thirdRowTop = rows.length > 2 ? rows[2] : Number.POSITIVE_INFINITY;
      setCapacity(chips.filter((_, i) => tops[i] < thirdRowTop).length);
    };
    measure();
    const ro = new ResizeObserver(() => measure());
    ro.observe(el);
    return () => ro.disconnect();
  }, [keySig]);

  const collapsible = capacity !== null && keys.length > capacity;
  const shown = expanded || capacity === null ? keys : orderedCategories(keys, selected);

  return (
    <div className="market-cats-row row" style={{ gap: 6, alignItems: "flex-start" }}>
      <div
        ref={wrapRef}
        className={`market-cats-wrap row wrap ${!expanded && collapsible ? "market-cats-collapsed" : ""}`}
        style={{ gap: 6 }}
        role="group"
        aria-label="分类筛选"
      >
        {shown.map((key) => (
          <button
            key={key}
            data-chip
            data-testid={`${testidPrefix}-${key}`}
            className={`chip ${selected === key ? "on" : ""}`}
            aria-pressed={selected === key}
            onClick={() => onSelect(key)}
          >
            {key === "all" ? "全部" : categories[key]?.zh || key}
          </button>
        ))}
      </div>
      {collapsible && (
        <button
          className="btn btn-ghost btn-sm market-cats-toggle"
          data-testid="market-cats-toggle"
          aria-expanded={expanded}
          aria-label={expanded ? "收起分类" : "展开全部分类"}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "收起 ▴" : `展开 (${keys.length}) ▾`}
        </button>
      )}
    </div>
  );
}

const SORT_OPTIONS: { value: string; label: string; spec: SortSpec }[] = [
  { value: "downloads:desc", label: "热度（下载多→少）", spec: { field: "downloads", dir: "desc" } },
  { value: "downloads:asc", label: "下载（少→多）", spec: { field: "downloads", dir: "asc" } },
  { value: "stars:desc", label: "星数（多→少）", spec: { field: "stars", dir: "desc" } },
  { value: "stars:asc", label: "星数（少→多）", spec: { field: "stars", dir: "asc" } },
  { value: "added:desc", label: "收录（新→旧）", spec: { field: "added", dir: "desc" } },
  { value: "added:asc", label: "收录（旧→新）", spec: { field: "added", dir: "asc" } },
];

function SortMenu({ sort, onChange }: { sort: SortSpec; onChange: (s: SortSpec) => void }) {
  const value = `${sort.field}:${sort.dir}`;
  return (
    <select
      className="input market-sort"
      data-testid="market-sort"
      aria-label="排序方式"
      value={value}
      onChange={(e) => {
        const opt = SORT_OPTIONS.find((o) => o.value === e.target.value);
        if (opt) onChange(opt.spec);
      }}
    >
      {SORT_OPTIONS.map((o) => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  );
}

export default function Plugins() {
  const market = useMarket();
  const { catalog, loading, error, busy, favorites } = market;
  // P2-6：IA 7 Tab + 查询语义（搜索词优先于分类；切 Tab 清洗失效筛选）
  const [query, setQuery] = useState(initialQuery);
  const tab = query.tab;
  const [sort, setSort] = useState<SortSpec>(() => loadSort());
  const [confirm, setConfirm] = useState<MarketEntry | null>(null);
  // 内置插件的启停是插件系统内部状态，改完需要一个刷新信号去重算启用计数
  const [, bumpBuiltin] = useState(0);
  const [backend, setBackend] = useState<BackendPlugin[] | null>(null);
  const [backendErr, setBackendErr] = useState("");

  useEffect(() => {
    void market.refresh();
    void api<{ plugins: BackendPlugin[] }>("/api/plugins")
      .then((d) => setBackend(d.plugins))
      .catch((e) => setBackendErr(String(e?.message || e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const plugins = catalog?.plugins ?? [];
  const installedCount = plugins.filter((p) => p.installed).length;
  const themeCount = plugins.filter((p) => p.kind === "theme").length;
  const toolCount = plugins.filter((p) => p.kind === "tool-pack").length;
  const dshCount = plugins.filter((p) => p.kind === "dsh-ref").length;
  const blockedCount = plugins.filter((p) => p.installed && !p.enabled).length;

  const switchTab = (key: TabKey) => {
    setQuery((q) => declaredQuery(q, key));
    void market.refresh(); // 切 Tab 即刷新，保持计数与启停状态最新
  };

  const changeSort = (s: SortSpec) => {
    setSort(s);
    saveSort(s);
  };

  // 过滤 + 排序：有搜索词时相关性优先；无词按所选排序
  const visible = useMemo(() => {
    const list = filterPlugins(plugins, query, favorites);
    if (query.text.trim()) {
      return [...list].sort(
        (a, b) => pluginRelevance(b, query.text) - pluginRelevance(a, query.text),
      );
    }
    if (tab === "discover" || tab === "dsh") {
      return [...list].sort((a, b) => comparePlugins(a, b, sort));
    }
    return list;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plugins, query, favorites, sort, tab]);

  const builtinStates = pluginStates();
  const hasFilters = Boolean(query.text.trim()) || query.category !== "all";
  const chipsVisible = tab === "discover" || tab === "dsh";

  /** 卡片的唯一渲染决策：可装 vs 只可浏览。所有 Tab 共用，避免各自演化。 */
  const renderEntry = (entry: MarketEntry) =>
    entry.kind === "dsh-ref" ? (
      <DshCard key={entry.id} entry={entry} fav={favorites.includes(entry.id)} onFav={() => market.toggleFav(entry.id)} />
    ) : (
      <MarketCard
        key={entry.id}
        entry={entry}
        fav={favorites.includes(entry.id)}
        busy={busy === entry.id}
        onInstall={() => setConfirm(entry)}
        onToggle={(enabled) => void market.toggle(entry.id, enabled)}
        onUninstall={() => void market.uninstall(entry.id)}
        onFav={() => market.toggleFav(entry.id)}
      />
    );

  return (
    <div className="page" data-testid="plugins-page">
      <div className="page-head row-between">
        <div>
          <h1 style={{ marginBottom: 2 }}>插件市场</h1>
          <p className="muted" style={{ margin: 0, fontSize: 13 }}>
            社区目录 {catalog ? `${catalog.count} 个插件 · 数据更新于 ${catalog.updated}` : "加载中…"}
            {catalog && dshCount > 0 ? `（含 DSH 社区 ${dshCount} 个，浏览型）` : ""}
            {" · "}安装只认市场目录，启停即时生效，可一步撤销
          </p>
        </div>
        <button className="btn btn-ghost btn-sm" data-testid="market-undo" onClick={() => void market.undo()}>
          ↩ 撤销上一步
        </button>
      </div>

      <div className="market-tabs" data-testid="market-tabs">
        {(
          [
            ["discover", `发现${catalog ? ` (${catalog.count})` : ""}`],
            ["themes", `主题 (${themeCount})`],
            ["tools", `工具 (${toolCount})`],
            ["dsh", `DSH 社区 (${dshCount})`],
            ["favorites", `收藏 (${favorites.length})`],
            ["installed", `已安装 (${installedCount})`],
            ["blocked", `停用 (${blockedCount})`],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            className={`market-tab ${tab === key ? "on" : ""}`}
            data-testid={`market-tab-${key}`}
            aria-label={label}
            onClick={() => switchTab(key)}
          >
            {label}
          </button>
        ))}
      </div>

      {(tab === "discover" || tab === "themes" || tab === "tools" || tab === "dsh" || tab === "favorites") && (
        <div className="market-toolbar">
          <input
            className="input market-search"
            data-testid="market-search"
            type="search"
            placeholder="搜索插件名、作者、描述…"
            aria-label="搜索插件"
            value={query.text}
            onChange={(e) => setQuery((q) => withText(q, e.target.value))}
          />
          {(tab === "discover" || tab === "dsh") && (
            <SortMenu sort={sort} onChange={changeSort} />
          )}
        </div>
      )}

      {chipsVisible && catalog && !query.text.trim() && (
        <CategoryChips
          categories={
            tab === "dsh"
              ? Object.fromEntries(Object.entries(catalog.categories).filter(([k]) => k.startsWith("dsh.")))
              : catalog.categories
          }
          selected={query.category}
          onSelect={(key) => setQuery((q) => ({ ...q, category: key }))}
          testidPrefix={`market-cat-${tab}`}
        />
      )}

      {error && <p className="muted">市场目录加载失败：{error}</p>}
      {loading && !catalog && (
        <div className="market-loading" data-testid="market-loading" role="status">
          <span className="market-logo-anim" aria-hidden><i /><i /><i /></span>
          正在加载插件目录…
        </div>
      )}

      {tab !== "installed" && (
        <div className="plugin-grid market-grid">
          {visible.map((entry) => renderEntry(entry))}
          {!loading && visible.length === 0 && catalog && (
            <div className="market-empty" data-testid="market-empty">
              <p className="muted" style={{ margin: 0 }}>
                {hasFilters ? "没有匹配的插件——可能被你的搜索词或分类筛选挡住了。" : "这里还没有内容。"}
              </p>
              {hasFilters && (
                <button
                  className="btn btn-ghost btn-sm"
                  data-testid="market-clear-filters"
                  onClick={() => setQuery((q) => ({ ...q, text: "", category: "all" }))}
                >
                  清除筛选
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {tab === "installed" && (
        <>
          <section className="card">
            <h3 style={{ marginTop: 0 }}>已安装的市场插件</h3>
            {installedCount === 0 ? (
              <p className="muted">
                还没有安装市场插件，去<a onClick={() => switchTab("discover")} style={{ cursor: "pointer" }}>发现</a>逛一逛。
              </p>
            ) : (
              <div className="plugin-grid market-grid">
                {plugins
                  .filter((p) => p.installed)
                  .map((entry) => renderEntry(entry))}
              </div>
            )}
          </section>

          <section className="card">
            <h3 style={{ marginTop: 0 }}>内置插件（改 UI）</h3>
            <div className="plugin-grid">
              {builtinStates.map(({ plugin, active }) => (
                <div key={plugin.manifest.id} className={`plugin-card ${active ? "on" : "off"}`} data-testid="plugin-card">
                  <div className="row-between">
                    <div>
                      <strong>{plugin.manifest.name}</strong>
                      <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>
                        {plugin.manifest.id} · v{plugin.manifest.version}
                      </span>
                    </div>
                    <button
                      className={`btn btn-sm ${active ? "btn-primary" : "btn-ghost"}`}
                      data-testid="plugin-toggle"
                      onClick={() => {
                        setPluginEnabled(plugin.manifest.id, !active);
                        bumpBuiltin((n) => n + 1);
                      }}
                    >
                      {active ? "已启用" : "已停用"}
                    </button>
                  </div>
                  {plugin.manifest.description && (
                    <p className="muted" style={{ margin: "6px 0", fontSize: 13 }}>{plugin.manifest.description}</p>
                  )}
                  <div className="row wrap" style={{ gap: 4 }}>
                    <span className="muted" style={{ fontSize: 12 }}>改这些地方：</span>
                    {(plugin.manifest.ui?.slots ?? []).map((s) => (
                      <span key={s} className="badge" title={SLOTS[s]?.desc || ""}>{s}</span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
            <p className="muted" style={{ marginBottom: 0, fontSize: 12 }} data-testid="builtin-enabled-count">
              当前启用 {builtinStates.filter((s) => isPluginEnabled(s.plugin.manifest.id)).length} / {builtinStates.length}
            </p>
          </section>

          <section className="card">
            <h3 style={{ marginTop: 0 }}>挂载点总览</h3>
            <p className="muted" style={{ marginTop: 0 }}>
              宿主授权的 UI 槽位全集。哪块界面能被插件改、现在被谁占着，一眼可见。
            </p>
            {Object.keys(SLOTS).map((slot) => (
              <SlotMapRow key={slot} slot={slot} />
            ))}
          </section>

          <section className="card">
            <h3 style={{ marginTop: 0 }}>后端插件（加能力）</h3>
            {backendErr && <p className="muted">加载失败：{backendErr}</p>}
            {!backend && !backendErr && <p className="muted">加载中…</p>}
            {backend?.length === 0 && <p className="muted">没有已加载的后端插件。</p>}
            {backend?.map((p) => (
              <div key={p.id} className="plugin-slot-row" data-testid="backend-plugin">
                <div className="row-between">
                  <div>
                    <strong>{p.name}</strong>
                    <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>{p.id} · v{p.version}</span>
                  </div>
                  <span className={`badge ${p.status === "active" ? "badge-ok" : ""}`}>{p.status}</span>
                </div>
                {p.description && <p className="muted" style={{ margin: "4px 0", fontSize: 12 }}>{p.description}</p>}
                <div className="row wrap" style={{ gap: 4 }}>
                  {(p.provides?.tools ?? []).map((t) => (
                    <span key={t} className="badge badge-ok">tool:{t}</span>
                  ))}
                </div>
              </div>
            ))}
          </section>
        </>
      )}

      {confirm && (
        <div className="modal-veil" data-testid="market-confirm">
          <div className="modal">
            <h3 style={{ marginTop: 0 }}>确认安装「{confirm.name}」</h3>
            <div className="muted market-byline" style={{ fontSize: 13 }}>
              <Avatar owner={confirm.owner} name={confirm.name} icon={confirm.icon} />
              <span>{confirm.owner}</span>
              <span>·</span>
              <span>v{confirm.version}</span>
              <span>·</span>
              <span>{KIND_LABEL[confirm.kind] || confirm.kind}</span>
              {confirm.added && (
                <>
                  <span>·</span>
                  <span>{confirm.added} 收录</span>
                </>
              )}
            </div>
            <p>{confirm.description?.zh}</p>
            <CapDisclosure entry={confirm} detailed />
            <div className="row wrap" style={{ gap: 4, marginTop: 8 }}>
              {confirm.kind === "tool-pack" && confirm.tools?.map((t) => (
                <span key={t} className="badge badge-ok">tool:{t}</span>
              ))}
              {confirm.kind === "ui-module" && <span className="badge">模块：{confirm.module}</span>}
            </div>
            <p className="muted" style={{ fontSize: 12, margin: "10px 0 0" }}>
              社区第三方内容；安装状态可在「已安装」里随时卸载，支持一步撤销。
            </p>
            <div className="row" style={{ gap: 8, marginTop: 16, justifyContent: "flex-end" }}>
              <button className="btn btn-ghost" onClick={() => setConfirm(null)}>取消</button>
              <button
                className="btn btn-primary"
                data-testid="market-confirm-ok"
                onClick={() => {
                  void market.install(confirm.id).then((ok) => ok && setConfirm(null));
                }}
              >
                安装
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
