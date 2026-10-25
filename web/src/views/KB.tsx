/**
 * 知识库（V4.0 N4）：集合 CRUD、文本文档入库、文件索引、检索测试、切块浏览。
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { SkeletonList } from "../components/Skeleton";
import { formatTime } from "../lib/format";
import { confirmDialog, openModal, toast, useSession } from "../store/app";

interface CollectionRow {
  id: string;
  name: string;
  desc: string;
  created_at: string;
  doc_count: number;
  chunk_count: number;
}

interface DocumentRow {
  id: string;
  file_id: string | null;
  name: string;
  chars: number;
  chunk_count: number;
  status: string;
  error: string;
  created_at: string;
}

interface ChunkRow {
  id: string;
  seq: number;
  heading: string;
  body_head: string;
  tokens: number;
}

interface HitRow {
  chunk_id: string;
  doc_id: string;
  seq: number;
  heading: string;
  body: string;
  score: number;
  snippet?: string;
  [k: string]: unknown;
}

export default function KB() {
  const { meta } = useSession();
  const [collections, setCollections] = useState<CollectionRow[]>([]);
  const [fts5, setFts5] = useState(true);
  const [activeId, setActiveId] = useState("");
  const [docs, setDocs] = useState<DocumentRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<HitRow[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [chunksDoc, setChunksDoc] = useState("");
  const [chunks, setChunks] = useState<ChunkRow[]>([]);
  const [docsVersion, setDocsVersion] = useState(0);

  const paint = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api<{ collections: CollectionRow[]; fts5: boolean }>("/api/rag/collections");
      setCollections(d.collections || []);
      setFts5(d.fts5);
      if (!activeId && d.collections?.length) setActiveId(d.collections[0].id);
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void paint();
  }, [paint]);

  useEffect(() => {
    if (!activeId) {
      setDocs([]);
      return;
    }
    api<{ documents: DocumentRow[] }>(`/api/rag/collections/${activeId}/documents`)
      .then((d) => setDocs(d.documents || []))
      .catch(() => setDocs([]));
  }, [activeId, docsVersion]);

  const reloadDocs = useCallback(() => setDocsVersion((v) => v + 1), []);

  useEffect(() => {
    if (!chunksDoc) {
      setChunks([]);
      return;
    }
    api<{ chunks: ChunkRow[] }>(`/api/rag/documents/${chunksDoc}/chunks?limit=50`)
      .then((d) => setChunks(d.chunks || []))
      .catch(() => setChunks([]));
  }, [chunksDoc]);

  function newCollection() {
    let name = "";
    let desc = "";
    openModal({
      title: "新建集合",
      body: (
        <div>
          <div className="field">
            <label htmlFor="col-name">名称</label>
            <input id="col-name" placeholder="例：行政法讲义" onChange={(e) => (name = e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="col-desc">说明（可选）</label>
            <input id="col-desc" placeholder="这个集合装什么" onChange={(e) => (desc = e.target.value)} />
          </div>
        </div>
      ),
      actions: [
        { label: "取消" },
        {
          label: "创建",
          kind: "btn-primary",
          onClick: async (close) => {
            if (!name.trim()) return;
            try {
              await api("/api/rag/collections", { method: "POST", body: { name: name.trim(), desc: desc.trim() } });
              close();
              toast("集合已建", "ok");
              await paint();
            } catch (err) {
              toast((err as Error).message, "error");
            }
          },
        },
      ],
    });
  }

  function addTextDoc() {
    if (!activeId) return;
    let name = "";
    let text = "";
    openModal({
      title: "添加文本文档",
      wide: true,
      body: (
        <div>
          <div className="field">
            <label htmlFor="doc-name">标题</label>
            <input id="doc-name" placeholder="例：第三讲 行政协议的效力" onChange={(e) => (name = e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="doc-text">正文（自动按 ~600 字切块）</label>
            <textarea id="doc-text" rows={12} placeholder="粘贴整篇文字…" onChange={(e) => (text = e.target.value)} />
          </div>
        </div>
      ),
      actions: [
        { label: "取消" },
        {
          label: "入库",
          kind: "btn-primary",
          onClick: async (close) => {
            if (!text.trim()) return;
            try {
              const r = await api<{ chunk_count: number }>(`/api/rag/collections/${activeId}/documents`, {
                method: "POST",
                body: { name: name.trim() || "未命名文档", text },
              });
              close();
              toast(`已入库，切成 ${r.chunk_count} 块`, "ok");
              await paint();
              reloadDocs();
            } catch (err) {
              toast((err as Error).message, "error");
            }
          },
        },
      ],
    });
  }

  async function runSearch() {
    if (!query.trim() || !activeId) return;
    setSearching(true);
    try {
      const d = await api<{ hits: HitRow[]; fts5: boolean }>("/api/rag/search", {
        method: "POST",
        body: { collection_ids: [activeId], query: query.trim(), top_k: 8 },
      });
      setHits(d.hits || []);
    } catch (err) {
      toast((err as Error).message, "error");
      setHits([]);
    } finally {
      setSearching(false);
    }
  }

  if (!meta?.rag?.enabled) {
    return (
      <div className="page">
        <div className="empty">
          <h4>知识库未启用</h4>
          <p>在 .env 里设 RAG_ENABLED=1 后重启服务。</p>
        </div>
      </div>
    );
  }

  const active = collections.find((c) => c.id === activeId);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>知识库</h1>
          <div className="page-sub">
            把材料切成片段存起来，对话时按问题检索注入。{fts5 ? "FTS5 可用" : "FTS5 不可用，退化为打分兜底"}
          </div>
        </div>
        <div className="row">
          <button className="btn btn-primary btn-sm" data-testid="kb-new" onClick={newCollection}>
            新建集合
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => void paint()}>
            刷新
          </button>
        </div>
      </div>

      {loading ? (
        <SkeletonList rows={3} />
      ) : collections.length === 0 ? (
        <div className="empty" data-testid="kb-empty">
          <h4>还没有集合</h4>
          <p>集合＝一个主题的材料堆。建一个，然后贴文字或索引已有文件。</p>
          <button className="btn btn-primary" onClick={newCollection}>
            新建集合
          </button>
        </div>
      ) : (
        <div className="kb-grid">
          <div className="kb-list" data-testid="kb-list">
            {collections.map((c) => (
              <button
                key={c.id}
                className={`kb-item ${activeId === c.id ? "active" : ""}`}
                data-testid="kb-item"
                onClick={() => {
                  setActiveId(c.id);
                  setHits(null);
                  setChunksDoc("");
                }}
              >
                <span className="kb-name truncate">{c.name}</span>
                <span className="chip-sub">
                  {c.doc_count} 篇 · {c.chunk_count} 块
                </span>
                {c.desc && <span className="tiny faint truncate">{c.desc}</span>}
              </button>
            ))}
          </div>

          <div className="kb-main">
            {active && (
              <>
                <div className="card" style={{ marginBottom: 14 }}>
                  <div className="row-between" style={{ marginBottom: 10 }}>
                    <h3 style={{ margin: 0, fontSize: 15 }}>{active.name}</h3>
                    <div className="row">
                      <button className="btn btn-sm" data-testid="kb-add-text" onClick={addTextDoc}>
                        贴文字
                      </button>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => {
                          let fid = "";
                          openModal({
                            title: "索引库里的文件",
                            body: (
                              <div className="field">
                                <label htmlFor="idx-fid">文件 id（在「库」页地址或列表里找）</label>
                                <input id="idx-fid" placeholder="f_..." onChange={(e) => (fid = e.target.value)} />
                              </div>
                            ),
                            actions: [
                              { label: "取消" },
                              {
                                label: "索引",
                                kind: "btn-primary",
                                onClick: async (close) => {
                                  if (!fid.trim()) return;
                                  try {
                                    await api("/api/rag/index-file", {
                                      method: "POST",
                                      body: { file_id: fid.trim(), collection_id: activeId },
                                    });
                                    close();
                                    toast("已索引", "ok");
                                    await paint();
                                    reloadDocs();
                                  } catch (err) {
                                    toast((err as Error).message, "error");
                                  }
                                },
                              },
                            ],
                          });
                        }}
                      >
                        索引文件
                      </button>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={async () => {
                          if (!(await confirmDialog(`删除集合「${active.name}」及其全部片段？`))) return;
                          await api(`/api/rag/collections/${activeId}`, { method: "DELETE" });
                          toast("已删除", "ok");
                          setActiveId("");
                          await paint();
                        }}
                      >
                        ×
                      </button>
                    </div>
                  </div>

                  <div className="row" style={{ gap: 8 }}>
                    <input
                      type="search"
                      placeholder="检索测试：按这个集合搜一段话…"
                      style={{ flex: 1 }}
                      data-testid="kb-search-input"
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && void runSearch()}
                    />
                    <button className="btn btn-primary btn-sm" data-testid="kb-search-btn" disabled={searching} onClick={() => void runSearch()}>
                      {searching ? "搜索中…" : "搜索"}
                    </button>
                  </div>

                  {hits !== null && (
                    <div style={{ marginTop: 12 }} data-testid="kb-hits">
                      {hits.length === 0 ? (
                        <p className="tiny faint" style={{ margin: 0 }}>
                          没命中。换个说法，或确认文档已入库。
                        </p>
                      ) : (
                        hits.map((h) => (
                          <div key={h.chunk_id} className="kb-hit">
                            <div className="row-between">
                              <span className="small" style={{ fontWeight: 600 }}>
                                {h.heading || String(h.doc_name || "")} #{h.seq}
                              </span>
                              <span className="tiny faint">分 {Number(h.score).toFixed(3)}</span>
                            </div>
                            <p className="tiny" style={{ margin: "4px 0 0" }}>
                              {String(h.snippet || h.body || "").slice(0, 200)}
                            </p>
                          </div>
                        ))
                      )}
                    </div>
                  )}
                </div>

                <div className="card">
                  <div className="aside-head" style={{ marginBottom: 8 }}>
                    <span>文档 {docs.length}</span>
                  </div>
                  {docs.length === 0 ? (
                    <p className="tiny faint" style={{ margin: 0 }}>
                      空集合。贴一段文字或索引一个文件。
                    </p>
                  ) : (
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th>标题</th>
                          <th>字数</th>
                          <th>切块</th>
                          <th>状态</th>
                          <th>时间</th>
                          <th />
                        </tr>
                      </thead>
                      <tbody>
                        {docs.map((d) => (
                          <tr key={d.id}>
                            <td className="truncate" style={{ maxWidth: 240 }} title={d.name}>
                              {d.name}
                            </td>
                            <td className="mono tiny">{d.chars}</td>
                            <td className="mono tiny">{d.chunk_count}</td>
                            <td>
                              <span className={`badge ${d.status === "done" ? "badge-ok" : "badge-warn"}`} title={d.error || ""}>
                                {d.status}
                              </span>
                            </td>
                            <td className="tiny faint">{formatTime(d.created_at)}</td>
                            <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                              <button className="btn btn-ghost btn-xs" onClick={() => setChunksDoc(chunksDoc === d.id ? "" : d.id)}>
                                切块
                              </button>
                              <button
                                className="btn btn-ghost btn-xs"
                                onClick={async () => {
                                  if (!(await confirmDialog("删除这篇文档？"))) return;
                                  await api(`/api/rag/documents/${d.id}`, { method: "DELETE" });
                                  toast("已删除", "ok");
                                  await paint();
                                  reloadDocs();
                                }}
                              >
                                ×
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}

                  {chunksDoc && (
                    <div style={{ marginTop: 12 }} data-testid="kb-chunks">
                      <div className="tiny faint" style={{ marginBottom: 6 }}>
                        切块预览（最多 50 条）
                      </div>
                      {chunks.map((c) => (
                        <div key={c.id} className="kb-hit">
                          <div className="row-between">
                            <span className="small" style={{ fontWeight: 600 }}>
                              #{c.seq} {c.heading || ""}
                            </span>
                            <span className="tiny faint">{c.tokens} tokens</span>
                          </div>
                          <p className="tiny" style={{ margin: "4px 0 0" }}>
                            {c.body_head}
                          </p>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
