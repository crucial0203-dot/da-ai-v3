/**
 * 工具页（V4.0 N6）：工具目录、启用开关、审批标记、真值表速查。
 * 开关与审批标记由管理员改；普通用户只读。
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { SkeletonList } from "../components/Skeleton";
import { openModal, toast, useSession } from "../store/app";

interface ToolRow {
  name: string;
  label: string;
  desc: string;
  schema: Record<string, unknown>;
  enabled: boolean;
  requires_approval: boolean;
  builtin: boolean;
  conversational: boolean;
}

interface GateTable {
  gates: Array<{ op: string; rows: Array<{ inputs: boolean[]; result: boolean }> }>;
}

export default function Tools() {
  const { me } = useSession();
  const isAdmin = me?.role === "admin";
  const [tools, setTools] = useState<ToolRow[]>([]);
  const [allowed, setAllowed] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);

  const paint = useCallback(async () => {
    setLoading(true);
    try {
      const d = await api<{ tools: ToolRow[]; allowed: string[] }>("/api/tools");
      setTools(d.tools || []);
      setAllowed(d.allowed || []);
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void paint();
  }, [paint]);

  async function patchTool(tool: ToolRow, patch: Partial<Pick<ToolRow, "enabled" | "requires_approval">>) {
    try {
      await api(`/api/tools/${tool.name}`, {
        method: "PATCH",
        body: {
          name: tool.name,
          enabled: patch.enabled ?? tool.enabled,
          requires_approval: patch.requires_approval ?? tool.requires_approval,
        },
      });
      await paint();
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  function showGates() {
    openModal({
      title: "逻辑门真值表",
      wide: true,
      body: <GateTableBody />,
      actions: [{ label: "关闭" }],
    });
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>工具</h1>
          <div className="page-sub">
            对话与工作流可调用的函数。标「需审批」的工具执行前会先问你。{!isAdmin && "（开关仅管理员可改）"}
          </div>
        </div>
        <div className="row">
          <button className="btn btn-ghost btn-sm" onClick={showGates}>
            逻辑门真值表
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => void paint()}>
            刷新
          </button>
        </div>
      </div>

      {loading ? (
        <SkeletonList rows={3} />
      ) : (
        <table className="data-table" data-testid="tools-table">
          <thead>
            <tr>
              <th>工具</th>
              <th>说明</th>
              <th>场景</th>
              <th>审批</th>
              <th>启用</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {tools.map((t) => (
              <tr key={t.name} data-testid="tool-row">
                <td>
                  <span style={{ fontWeight: 600 }}>{t.label || t.name}</span>
                  <span className="tiny faint mono" style={{ display: "block" }}>
                    {t.name}
                  </span>
                </td>
                <td className="tiny" style={{ maxWidth: 320 }}>
                  {t.desc}
                </td>
                <td>
                  <span className="badge">{t.conversational ? "对话" : "工作流"}</span>
                  {t.builtin && <span className="badge" style={{ marginLeft: 4 }}>内置</span>}
                </td>
                <td>
                  {t.requires_approval ? (
                    <span className="badge badge-warn" data-testid="tool-approval-badge">需审批</span>
                  ) : (
                    <span className="tiny faint">直接执行</span>
                  )}
                </td>
                <td>
                  <button
                    className={`switch ${t.enabled ? "on" : ""}`}
                    data-testid="tool-switch"
                    disabled={!isAdmin}
                    aria-label="启用或停用"
                    onClick={() => void patchTool(t, { enabled: !t.enabled })}
                  />
                </td>
                <td style={{ textAlign: "right" }}>
                  <button
                    className="btn btn-ghost btn-xs"
                    onClick={() =>
                      openModal({
                        title: t.label || t.name,
                        wide: true,
                        body: (
                          <div>
                            <p className="small">{t.desc}</p>
                            <div className="tiny faint" style={{ marginBottom: 6 }}>
                              参数 schema
                            </div>
                            <pre style={{ maxHeight: "40vh", overflow: "auto" }}>{JSON.stringify(t.schema, null, 2)}</pre>
                          </div>
                        ),
                        actions: [{ label: "关闭" }],
                      })
                    }
                  >
                    schema
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <p className="tiny faint" style={{ marginTop: 14 }}>
        可进入对话白名单的工具：{allowed.join("、") || "（无）"}
      </p>
    </div>
  );
}

function GateTableBody() {
  const [gates, setGates] = useState<GateTable["gates"]>([]);
  useEffect(() => {
    api<GateTable>("/api/tools/gates")
      .then((d) => setGates(d.gates || []))
      .catch(() => setGates([]));
  }, []);
  if (!gates.length) return <p className="tiny faint">加载中…</p>;
  return (
    <div className="gate-tables" data-testid="gate-tables">
      {gates.map((g) => (
        <div key={g.op} style={{ marginBottom: 16 }}>
          <h4 style={{ margin: "0 0 6px", fontSize: 14 }}>{g.op}</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>入1</th>
                <th>入2</th>
                <th>入3</th>
                <th>入4</th>
                <th>结果</th>
              </tr>
            </thead>
            <tbody>
              {g.rows.map((r, i) => (
                <tr key={i}>
                  {r.inputs.map((v, j) => (
                    <td key={j} className="mono tiny">
                      {v ? "T" : "F"}
                    </td>
                  ))}
                  <td className="mono tiny" style={{ fontWeight: 700 }}>
                    {r.result ? "T" : "F"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}
