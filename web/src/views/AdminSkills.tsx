/** 技能配置中心：workflow 技能的草稿/发布/停用/回滚。 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import { api, ApiError } from "../lib/api";
import { formatTime } from "../lib/format";
import { confirmDialog, openModal, toast, useSession } from "../store/app";

const TOOL_LIST: Array<[string, string]> = [
  ["file.read", "读任务材料"],
  ["reference.verify", "资料核验"],
  ["minimax.generate", "模型调用"],
  ["pptx.create", "创建 PPTX"],
  ["pptx.modify", "修改 PPTX"],
  ["pptx.validate", "验收检查"],
  ["artifact.write", "登记交付物"],
];
const FORBIDDEN = ["shell.execute", "secret.read"];

interface NodeMeta {
  key: string;
  label: string;
  deps: string[];
  tools: string[];
  retries: number;
}
interface SkillMeta {
  name: string;
  description: string;
  nodes: NodeMeta[];
  permissions: Record<string, boolean>;
  params_schema: Record<string, unknown>;
  policy: { approval_after: string | null; max_retries_per_step: number };
}
interface SkillDetail {
  id: string;
  name: string;
  category: string;
  enabled: boolean;
  published_version: string | null;
  has_draft: boolean;
  draft: { body: string; meta: SkillMeta } | null;
  published: { body: string; meta: SkillMeta } | null;
  versions: Array<{ version: string; status: string; created_at: string }>;
}
interface SkillRow {
  id: string;
  name: string;
  enabled: boolean;
  published_version: string | null;
  has_draft: boolean;
}

const EMPTY_META = (): SkillMeta => ({
  name: "",
  description: "",
  nodes: [],
  permissions: Object.fromEntries(TOOL_LIST.map(([t]) => [t, true])),
  params_schema: {},
  policy: { approval_after: null, max_retries_per_step: 2 },
});

function configError(err: unknown) {
  const detail = err instanceof ApiError ? (err.detail as { errors?: string[] } | undefined) : undefined;
  if (detail && Array.isArray(detail.errors)) {
    toast(detail.errors[0], "error", 5000);
    openModal({
      title: "配置校验未通过",
      body: (
        <ul className="small">
          {detail.errors.map((e, i) => (
            <li key={i}>{e}</li>
          ))}
        </ul>
      ),
      actions: [{ label: "知道了" }],
    });
  } else {
    toast((err as Error).message || "操作失败", "error");
  }
}

export default function AdminSkills() {
  const me = useSession((s) => s.me);
  const [rows, setRows] = useState<SkillRow[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  // 编辑态（直接持有 draft.meta 的可变副本）
  const [meta, setMeta] = useState<SkillMeta>(EMPTY_META());
  const [body, setBody] = useState("");

  const paintList = useCallback(async (selectId?: string) => {
    const { skills } = await api<{ skills: SkillRow[] }>("/api/admin/skills");
    setRows(skills);
    if (selectId) await openSkill(selectId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openSkill = useCallback(async (id: string) => {
    setCurrentId(id);
    try {
      const d = await api<SkillDetail>(`/api/admin/skills/${id}`);
      setDetail(d);
      const draft = d.draft || (d.published ? { ...d.published } : null);
      const m = draft && draft.meta && Object.keys(draft.meta).length ? draft.meta : EMPTY_META();
      setMeta(JSON.parse(JSON.stringify(m)));
      setBody(draft ? draft.body : "");
    } catch (err) {
      toast((err as Error).message || "读不到技能", "error");
    }
  }, []);

  useEffect(() => {
    if (me?.role === "admin") void paintList();
  }, [me, paintList]);

  if (me?.role !== "admin") {
    return (
      <div className="page">
        <div className="empty">
          <h4>没有权限</h4>
          <p>仅管理员可见。</p>
        </div>
      </div>
    );
  }

  const readonly = detail ? !detail.has_draft && !!detail.published : true;

  async function saveDraft() {
    if (!currentId) return;
    if (!meta.nodes.length) {
      toast("至少要有一个节点", "error");
      return;
    }
    try {
      await api(`/api/admin/skills/${currentId}/draft`, { method: "PUT", body: { body, meta } });
      toast("草稿已保存", "ok");
      await openSkill(currentId);
      await paintList();
    } catch (err) {
      configError(err);
    }
  }

  async function publish() {
    if (!currentId) return;
    if (!meta.nodes.length) {
      toast("至少要有一个节点", "error");
      return;
    }
    try {
      await api(`/api/admin/skills/${currentId}/draft`, { method: "PUT", body: { body, meta } });
    } catch (err) {
      configError(err);
      return;
    }
    if (!(await confirmDialog("发布新版本？发布后不可原地修改。"))) return;
    try {
      const res = await api<{ version: string }>(`/api/admin/skills/${currentId}/publish`, { method: "POST" });
      toast(`已发布 ${res.version}`, "ok");
      await openSkill(currentId);
      await paintList();
    } catch (err) {
      configError(err);
    }
  }

  function newSkill() {
    let name = "";
    let desc = "";
    openModal({
      title: "新建 workflow 技能",
      body: (
        <>
          <div className="field">
            <label>名称</label>
            <input id="ns-name" type="text" placeholder="如 PPT 制作工作流" onChange={(e) => (name = e.target.value)} />
          </div>
          <div className="field">
            <label>说明</label>
            <input id="ns-desc" type="text" onChange={(e) => (desc = e.target.value)} />
          </div>
        </>
      ),
      actions: [
        { label: "取消" },
        {
          label: "创建（落草稿）",
          kind: "btn-primary",
          onClick: async (close) => {
            if (!name.trim()) {
              toast("填个名字", "error");
              return;
            }
            const m = EMPTY_META();
            m.name = name.trim();
            m.description = desc.trim();
            m.nodes = [
              { key: "plan", label: "任务规划", deps: [], tools: ["minimax.generate"], retries: 2 },
              { key: "render", label: "制作", deps: ["plan"], tools: ["pptx.create", "artifact.write"], retries: 2 },
            ];
            try {
              const res = await api<{ skill: { id: string } }>("/api/admin/skills", {
                method: "POST",
                body: {
                  name: name.trim(),
                  category: "工作流",
                  description: m.description,
                  body: `# ${name.trim()}\n\n（在这里写这份技能的指令。）\n`,
                  meta: m,
                },
              });
              close();
              toast("已创建，进入编辑", "ok");
              await paintList(res.skill.id);
            } catch (err) {
              configError(err);
            }
          },
        },
      ],
    });
  }

  return (
    <div className="page" style={{ maxWidth: 1100 }}>
      <div className="page-head">
        <div>
          <h1>技能配置中心</h1>
          <div className="page-sub">skill.json + SKILL.md，版本化发布；发布版本不可原地修改</div>
        </div>
        <div className="row">
          <Link className="btn btn-ghost btn-sm" to="/admin">
            数据
          </Link>
          <button className="btn btn-sm btn-primary" onClick={newSkill}>
            ＋ 新技能
          </button>
        </div>
      </div>

      <div className="skills-admin-grid">
        <aside className="skills-admin-list">
          {rows.length === 0 && <p className="small faint">还没有 workflow 技能。点右上「新技能」。</p>}
          {rows.map((s) => (
            <button key={s.id} className={`skill-admin-item ${currentId === s.id ? "active" : ""}`} onClick={() => void openSkill(s.id)}>
              <span style={{ fontWeight: 600, fontSize: 13, display: "block" }}>{s.name}</span>
              <span className="row-between tiny faint">
                <span className={`badge ${s.published_version ? "badge-ok" : "badge-warn"}`}>
                  {s.published_version ? `已发布 ${s.published_version}` : "未发布"}
                </span>
                <span>{s.has_draft ? "有草稿" : ""}</span>
              </span>
            </button>
          ))}
        </aside>

        <section>
          {!detail ? (
            <p className="small faint">从左侧选一个技能。</p>
          ) : (
            <div className="section">
              <div className="row-between" style={{ marginBottom: 12 }}>
                <h4 style={{ margin: 0, display: "flex", gap: 8, alignItems: "center" }}>
                  {detail.name}
                  {detail.published_version ? (
                    <span className="badge badge-ok">发布 {detail.published_version}</span>
                  ) : (
                    <span className="badge badge-warn">未发布</span>
                  )}
                  {detail.has_draft && <span className="badge">草稿</span>}
                  {!detail.enabled && <span className="badge">已停用</span>}
                </h4>
                <div className="row">
                  {readonly && (
                    <button
                      className="btn btn-sm"
                      onClick={async () => {
                        await api(`/api/admin/skills/${detail.id}/draft`, {
                          method: "PUT",
                          body: { body: detail.published?.body || "", meta: detail.published?.meta || EMPTY_META() },
                        });
                        toast("草稿已创建", "ok");
                        await openSkill(detail.id);
                        await paintList();
                      }}
                    >
                      从当前版本建草稿
                    </button>
                  )}
                  <button className="btn btn-sm" disabled={!detail.has_draft} onClick={() => void saveDraft()}>
                    保存草稿
                  </button>
                  <button className="btn btn-sm btn-primary" disabled={!detail.has_draft} onClick={() => void publish()}>
                    发布新版本
                  </button>
                  <button
                    className="btn btn-sm btn-ghost"
                    onClick={async () => {
                      await api(`/api/admin/skills/${detail.id}/disable?enabled=${detail.enabled}`);
                      toast(detail.enabled ? "已停用" : "已启用", "ok");
                      await openSkill(detail.id);
                      await paintList();
                    }}
                  >
                    {detail.enabled ? "停用" : "启用"}
                  </button>
                </div>
              </div>

              <details className="admin-sec" open>
                <summary>基础信息</summary>
                <div className="form-row">
                  <div>
                    <span className="small">名称</span>
                  </div>
                  <div>
                    <input value={meta.name || detail.name} disabled={readonly} onChange={(e) => setMeta({ ...meta, name: e.target.value })} />
                  </div>
                </div>
                <div className="form-row">
                  <div>
                    <span className="small">说明</span>
                  </div>
                  <div>
                    <input value={meta.description} disabled={readonly} onChange={(e) => setMeta({ ...meta, description: e.target.value })} />
                  </div>
                </div>
              </details>

              <details className="admin-sec" open>
                <summary>指令 SKILL.md</summary>
                <textarea className="mono" rows={14} style={{ width: "100%" }} value={body} disabled={readonly} onChange={(e) => setBody(e.target.value)} />
                <p className="tiny faint">正文只影响模型怎么写；工具能力以「流程与权限」里勾选的为准。</p>
              </details>

              <details className="admin-sec" open>
                <summary>流程与权限</summary>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>key</th>
                      <th>名称</th>
                      <th>依赖</th>
                      <th>工具</th>
                      <th>重试</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {meta.nodes.map((n, i) => (
                      <tr key={i}>
                        <td>
                          <input
                            value={n.key}
                            style={{ width: 90 }}
                            disabled={readonly}
                            onChange={(e) =>
                              setMeta({ ...meta, nodes: meta.nodes.map((x, j) => (j === i ? { ...x, key: e.target.value.trim() } : x)) })
                            }
                          />
                        </td>
                        <td>
                          <input
                            value={n.label}
                            style={{ width: 110 }}
                            disabled={readonly}
                            onChange={(e) =>
                              setMeta({ ...meta, nodes: meta.nodes.map((x, j) => (j === i ? { ...x, label: e.target.value.trim() } : x)) })
                            }
                          />
                        </td>
                        <td>
                          <input
                            value={n.deps.join(",")}
                            placeholder="如 plan"
                            style={{ width: 90 }}
                            disabled={readonly}
                            onChange={(e) =>
                              setMeta({
                                ...meta,
                                nodes: meta.nodes.map((x, j) =>
                                  j === i ? { ...x, deps: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) } : x,
                                ),
                              })
                            }
                          />
                        </td>
                        <td>
                          <div className="row wrap" style={{ gap: 4 }}>
                            {TOOL_LIST.map(([tool, label]) => (
                              <label key={tool} className="tiny" style={{ whiteSpace: "nowrap" }}>
                                <input
                                  type="checkbox"
                                  checked={(n.tools || []).includes(tool)}
                                  disabled={readonly}
                                  onChange={(e) =>
                                    setMeta({
                                      ...meta,
                                      nodes: meta.nodes.map((x, j) => {
                                        if (j !== i) return x;
                                        const set = new Set(x.tools || []);
                                        if (e.target.checked) set.add(tool);
                                        else set.delete(tool);
                                        return { ...x, tools: [...set] };
                                      }),
                                    })
                                  }
                                />{" "}
                                {label}
                              </label>
                            ))}
                          </div>
                        </td>
                        <td>
                          <input
                            type="number"
                            min={0}
                            max={5}
                            value={n.retries ?? 2}
                            style={{ width: 56 }}
                            disabled={readonly}
                            onChange={(e) =>
                              setMeta({ ...meta, nodes: meta.nodes.map((x, j) => (j === i ? { ...x, retries: Number(e.target.value) || 0 } : x)) })
                            }
                          />
                        </td>
                        <td>
                          {!readonly && (
                            <button className="btn btn-ghost btn-sm" onClick={() => setMeta({ ...meta, nodes: meta.nodes.filter((_, j) => j !== i) })}>
                              删
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!readonly && (
                  <button
                    className="btn btn-sm"
                    style={{ marginTop: 8 }}
                    onClick={() =>
                      setMeta({
                        ...meta,
                        nodes: [...meta.nodes, { key: `node${meta.nodes.length + 1}`, label: "新节点", deps: [], tools: ["minimax.generate"], retries: 2 }],
                      })
                    }
                  >
                    ＋ 加节点
                  </button>
                )}

                <div className="form-row" style={{ marginTop: 12 }}>
                  <div>
                    <span className="small">工具权限</span>
                    <div className="hint">{FORBIDDEN.join(" / ")} 平台级禁止，配置了也无效</div>
                  </div>
                  <div className="row wrap">
                    {TOOL_LIST.map(([tool, label]) => (
                      <label key={tool} className="tiny" style={{ whiteSpace: "nowrap" }}>
                        <input
                          type="checkbox"
                          checked={Boolean(meta.permissions?.[tool])}
                          disabled={readonly}
                          onChange={(e) => setMeta({ ...meta, permissions: { ...meta.permissions, [tool]: e.target.checked } })}
                        />{" "}
                        {label}
                      </label>
                    ))}
                  </div>
                </div>
                <div className="form-row">
                  <div>
                    <span className="small">审批关卡</span>
                    <div className="hint">该节点完成后暂停，等用户确认</div>
                  </div>
                  <div>
                    <select
                      value={meta.policy?.approval_after || ""}
                      disabled={readonly}
                      onChange={(e) => setMeta({ ...meta, policy: { ...meta.policy, approval_after: e.target.value || null } })}
                    >
                      <option value="">不设审批</option>
                      {meta.nodes
                        .filter((n) => n.key)
                        .map((n) => (
                          <option key={n.key} value={n.key}>
                            {n.key} 完成后
                          </option>
                        ))}
                    </select>
                  </div>
                </div>
                <div className="form-row">
                  <div>
                    <span className="small">单步重试上限</span>
                  </div>
                  <div>
                    <input
                      type="number"
                      min={0}
                      max={5}
                      value={meta.policy?.max_retries_per_step ?? 2}
                      disabled={readonly}
                      onChange={(e) => setMeta({ ...meta, policy: { ...meta.policy, max_retries_per_step: Number(e.target.value) || 0 } })}
                      style={{ width: 80 }}
                    />
                  </div>
                </div>
              </details>

              <details className="admin-sec">
                <summary>版本记录</summary>
                {detail.versions.length === 0 ? (
                  <p className="tiny faint">还没有版本记录。</p>
                ) : (
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>版本</th>
                        <th>状态</th>
                        <th>时间</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {detail.versions.map((v) => (
                        <tr key={v.version}>
                          <td className="mono tiny">{v.version}</td>
                          <td>
                            <span className={`badge ${v.status === "published" ? "badge-ok" : v.status === "draft" ? "badge-warn" : ""}`}>
                              {v.status === "published" ? "发布中" : v.status === "draft" ? "草稿" : "已归档"}
                            </span>
                          </td>
                          <td className="tiny faint">{formatTime(v.created_at)}</td>
                          <td style={{ textAlign: "right" }}>
                            {v.status !== "draft" && v.version !== detail.published_version && (
                              <button
                                className="btn btn-ghost btn-sm"
                                onClick={async () => {
                                  if (!(await confirmDialog(`回滚到 ${v.version}？发布指针将指向该版本。`))) return;
                                  try {
                                    await api(`/api/admin/skills/${detail.id}/rollback`, { method: "POST", body: { version: v.version } });
                                    toast("已回滚", "ok");
                                    await openSkill(detail.id);
                                    await paintList();
                                  } catch (err) {
                                    toast((err as Error).message, "error");
                                  }
                                }}
                              >
                                回滚到此版
                              </button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </details>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
