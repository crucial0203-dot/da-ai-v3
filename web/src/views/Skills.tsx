/** 技能中心（Open WebUI Discover 布局）：搜索 + 分类筛选 + 卡片网格，一键启停。 */
import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { SkeletonCard } from "../components/Skeleton";
import { formatTime } from "../lib/format";
import { plainPreview } from "../lib/md";
import { confirmDialog, openModal, toast, useSession } from "../store/app";

interface SkillRow {
  id: string;
  name: string;
  version: string;
  kind: string;
  kind_label: string;
  category: string;
  enabled: boolean;
  reviewed: boolean;
  is_builtin: boolean;
  use_count?: number;
  created_at: string;
  body_preview: string;
  body_len: number;
}

const KIND_NOTE: Record<string, string> = {
  prompt: "只注入正文，不新增能力。",
  workflow: "只注入文本，不再新增节点。",
  tool: "本版未开放。",
  script: "只保存，不执行。",
};

export default function Skills() {
  const me = useSession((s) => s.me);
  const [skills, setSkills] = useState<SkillRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState("");
  const [category, setCategory] = useState("");

  async function load() {
    setLoading(true);
    try {
      const d = await api<{ skills: SkillRow[] }>("/api/skills");
      setSkills(d.skills);
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const categories = useMemo(() => ["", ...Array.from(new Set(skills.map((s) => s.category)).values())], [skills]);
  const shown = useMemo(() => {
    const q = keyword.trim().toLowerCase();
    return skills.filter(
      (s) => (!category || s.category === category) && (!q || s.name.toLowerCase().includes(q) || s.body_preview.toLowerCase().includes(q)),
    );
  }, [skills, keyword, category]);

  async function toggle(s: SkillRow) {
    try {
      await api(`/api/skills/${s.id}`, { method: "PATCH", body: { enabled: !s.enabled } });
      setSkills((rows) => rows.map((r) => (r.id === s.id ? { ...r, enabled: !r.enabled } : r)));
      toast(!s.enabled ? "已启用" : "已停用", "ok");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  async function openDetail(id: string) {
    try {
      const skill = await api<SkillRow & { body: string }>(`/api/skills/${id}`);
      openModal({
        title: skill.name,
        wide: true,
        body: (
          <>
            <div className="row" style={{ marginBottom: 12 }}>
              <span className="badge badge-ink">{skill.kind_label}</span>
              <span className="tiny faint">
                {skill.category} · v{skill.version} · {skill.body_len} 字
                {typeof skill.use_count === "number" ? ` · 已用 ${skill.use_count} 次` : ""}
              </span>
            </div>
            <p className="small muted" style={{ marginTop: 0 }}>
              {KIND_NOTE[skill.kind] || ""}
            </p>
            <pre style={{ maxHeight: "48vh", overflow: "auto", whiteSpace: "pre-wrap" }}>{skill.body}</pre>
          </>
        ),
        actions: [{ label: "关闭" }],
      });
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  function openUpload() {
    let picked: File | null = null;
    let name = "";
    let cat = "通用";
    let bodyText = "";
    openModal({
      title: "上传技能",
      body: (
        <>
          <div className="field">
            <label htmlFor="skill-file">SKILL.md 或 .zip 包</label>
            <input
              id="skill-file"
              type="file"
              accept=".md,.txt,.zip"
              onChange={(e) => (picked = e.target.files?.[0] || null)}
            />
            <div className="hint">含 .py/.js/.sh 的包会被标为「含脚本」，只保存不执行。</div>
          </div>
          <div className="field">
            <label htmlFor="skill-name">名称（留空则取文件里的标题）</label>
            <input id="skill-name" type="text" placeholder="例如：我的读书笔记法" onChange={(e) => (name = e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="skill-cat">分类</label>
            <select id="skill-cat" onChange={(e) => (cat = e.target.value)}>
              <option>通用</option>
              <option>写作</option>
              <option>研究</option>
              <option>演示</option>
              <option>文书</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="skill-body">或粘贴正文</label>
            <textarea id="skill-body" rows={6} placeholder="支持 Markdown 与 front-matter（name/version）" onChange={(e) => (bodyText = e.target.value)} />
          </div>
        </>
      ),
      actions: [
        { label: "取消" },
        {
          label: "上传",
          kind: "btn-primary",
          onClick: async (close) => {
            const form = new FormData();
            if (picked) form.append("file", picked);
            if (name.trim()) form.append("name", name.trim());
            form.append("category", cat);
            if (bodyText.trim()) form.append("body", bodyText.trim());
            if (!picked && !bodyText.trim()) {
              toast("请选择文件或填写正文", "error");
              return;
            }
            const res = await fetch("/api/skills", { method: "POST", body: form, credentials: "same-origin" });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
              toast((data as { detail?: string }).detail || "上传失败", "error");
              return;
            }
            close();
            await load();
            toast(`已加入：${(data as { skill: { name: string; kind_label: string } }).skill.name}`, "ok");
          },
        },
      ],
    });
  }

  return (
    <div className="page">
      <div className="discover-head">
        <h1>技能</h1>
        <p className="page-sub">写作时按你的方法来。开启即在下一次对话生效。</p>
        <div className="discover-search">
          <input type="search" placeholder="搜技能…" value={keyword} onChange={(e) => setKeyword(e.target.value)} data-testid="skill-search" />
        </div>
        <div className="filter-chips" data-testid="skill-filters">
          {categories.map((c) => (
            <button key={c || "all"} className={`filter-chip ${category === c ? "active" : ""}`} onClick={() => setCategory(c)}>
              {c || "全部"}
            </button>
          ))}
        </div>
      </div>

      <div className="row-between" style={{ marginBottom: 16 }}>
        <span className="small faint">
          {loading ? "读取中…" : `${shown.length} 个技能`}
        </span>
        <button className="btn btn-primary btn-sm" onClick={openUpload}>
          ＋ 上传技能
        </button>
      </div>

      {loading ? (
        <SkeletonCard cards={4} />
      ) : shown.length === 0 ? (
        <div className="empty">
          <h4>还没有技能</h4>
          <p>上传一份 SKILL.md，或直接粘贴一段方法说明。</p>
        </div>
      ) : (
        <div className="skill-grid" data-testid="skill-grid">
          {shown.map((s) => (
            <article key={s.id} className="skill-card" data-testid="skill-card">
              <div className="row-between">
                <h4 onClick={() => void openDetail(s.id)}>{s.name}</h4>
                {s.is_builtin ? (
                  <span className="badge">内置</span>
                ) : (
                  <span className={`badge ${s.reviewed ? "badge-ok" : ""}`}>{s.reviewed ? "已审核" : "私有"}</span>
                )}
              </div>
              <div className="row">
                <span className="badge badge-ink" title={KIND_NOTE[s.kind] || ""}>
                  {s.kind_label}
                </span>
                <span className="tiny faint">
                  {s.category} · v{s.version}
                </span>
                {typeof s.use_count === "number" && s.use_count > 0 && <span className="tiny faint">· 已用 {s.use_count} 次</span>}
              </div>
              <p className="desc">{plainPreview(s.body_preview, 90)}</p>
              <div className="row-between">
                <span className="tiny faint">
                  {s.body_len} 字 · {formatTime(s.created_at)}
                </span>
                <div className="row" style={{ gap: 6 }}>
                  {me?.role === "admin" && !s.reviewed && !s.is_builtin && (
                    <button
                      className="btn btn-ghost btn-sm"
                      onClick={async () => {
                        await api(`/api/skills/${s.id}/review`, { method: "POST" });
                        await load();
                        toast("已审核并设为共享", "ok");
                      }}
                    >
                      审核
                    </button>
                  )}
                  {!s.is_builtin && (
                    <button
                      className="btn btn-ghost btn-sm"
                      onClick={async () => {
                        if (!(await confirmDialog("删除这个技能？已用它写的稿子不受影响。"))) return;
                        await api(`/api/skills/${s.id}`, { method: "DELETE" });
                        await load();
                        toast("已删除", "ok");
                      }}
                    >
                      删除
                    </button>
                  )}
                  <button className={`switch ${s.enabled ? "on" : ""}`} aria-label="启用或停用" data-testid="skill-switch" onClick={() => void toggle(s)} />
                </div>
              </div>
            </article>
          ))}
        </div>
      )}

      <p className="tiny faint" style={{ marginTop: 32 }}>
        正文注入对话；含 <span className="mono">.py / .js / .sh</span> 的包只保存，不执行。公开需管理员审核。
      </p>
    </div>
  );
}
