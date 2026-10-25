/** 管理台：概览、邀请码、访客、操作记录。 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import { api } from "../lib/api";
import { formatNumber, formatTime } from "../lib/format";
import { SkeletonCard, SkeletonList } from "../components/Skeleton";
import { confirmDialog, openModal, toast, useSession } from "../store/app";

interface Overview {
  visitors: number;
  tasks: number;
  files: number;
  file_bytes: number;
  today: { calls: number; in: number; out: number };
  model: string;
  key_configured: boolean;
  invites: Array<{
    code: string;
    state: string;
    used: number;
    max_uses: number;
    daily_quota: number | null;
    expires_at: string;
    joined: number;
    disabled: boolean;
  }>;
}
interface VisitorRow {
  id: string;
  nickname: string;
  invite_code: string;
  calls_today: number;
  tasks: number;
  quota_override: number | null;
  invite_quota: number | null;
  last_seen: string;
  disabled: boolean;
}

const STATE_LABEL: Record<string, [string, string]> = {
  ok: ["可用", "badge-ok"],
  used_up: ["已用完", "badge-warn"],
  expired: ["已过期", "badge-warn"],
  disabled: ["已停用", ""],
};

export default function Admin() {
  const me = useSession((s) => s.me);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [visitors, setVisitors] = useState<VisitorRow[]>([]);
  const [audit, setAudit] = useState<Array<{ created_at: string; actor: string; action: string; target: string }>>([]);

  const paint = useCallback(async () => {
    try {
      const [ov, vs, au] = await Promise.all([
        api<Overview>("/api/admin/overview"),
        api<{ visitors: VisitorRow[] }>("/api/admin/visitors"),
        api<{ items: Array<{ created_at: string; actor: string; action: string; target: string }> }>("/api/admin/audit?limit=60"),
      ]);
      setOverview(ov);
      setVisitors(vs.visitors);
      setAudit(au.items);
    } catch (err) {
      toast((err as Error).message || "读不到管理数据", "error");
    }
  }, []);

  useEffect(() => {
    void paint();
  }, [paint]);

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

  async function genInvite() {
    const count = Number((document.getElementById("invite-count") as HTMLInputElement)?.value) || 1;
    const maxUses = Number((document.getElementById("invite-uses") as HTMLInputElement)?.value) || 5;
    const days = Number((document.getElementById("invite-days") as HTMLInputElement)?.value) || 7;
    const dailyQuota = Number((document.getElementById("invite-quota") as HTMLInputElement)?.value) || 50;
    try {
      const data = await api<{ codes: string[] }>("/api/admin/invites", {
        method: "POST",
        body: { count: Math.min(10, Math.max(1, count)), max_uses: Math.min(100, Math.max(1, maxUses)), days: Math.min(90, Math.max(1, days)), daily_quota: Math.min(500, Math.max(1, dailyQuota)) },
      });
      openModal({
        title: "新邀请码",
        body: (
          <>
            <p className="small muted">
              每个码可进 {maxUses} 人，{days} 天内有效，每人每天 {dailyQuota} 次。发给朋友即可，不需要密码。
            </p>
            <pre style={{ fontSize: 15, letterSpacing: "0.08em" }}>{data.codes.join("\n")}</pre>
          </>
        ),
        actions: [{ label: "关闭" }],
      });
      await paint();
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>管理台</h1>
          <div className="page-sub">邀请码 · 访客 · 用量</div>
        </div>
        <div className="row">
          <Link className="btn btn-ghost btn-sm" to="/admin/skills">
            技能配置
          </Link>
          <button className="btn btn-ghost btn-sm" onClick={() => void paint()}>
            刷新
          </button>
        </div>
      </div>

        {!overview ? (
          <SkeletonCard cards={5} />
        ) : (
          <div className="row wrap" style={{ gap: 12, marginBottom: 24 }}>
          {[
            ["访客", overview.visitors, `共 ${overview.invites.length} 个邀请码`],
            ["任务", overview.tasks, ""],
            ["文件", overview.files, `${(overview.file_bytes / 1024 / 1024).toFixed(1)} MB`],
            ["今日调用", overview.today.calls, `in ${formatNumber(overview.today.in)} / out ${formatNumber(overview.today.out)}`],
            ["模型", overview.model, overview.key_configured ? "Key 已配置" : "Key 未配置"],
          ].map(([label, value, hint]) => (
            <div key={String(label)} className="card" style={{ flex: 1, minWidth: 140 }}>
              <div className="tiny faint">{label}</div>
              <div className="mono" style={{ fontSize: 17, marginTop: 2 }}>
                {String(value ?? "—")}
              </div>
              <div className="tiny faint">{hint}</div>
            </div>
          ))}
        </div>
      )}

      <section className="section">
        <div className="row-between" style={{ marginBottom: 10 }}>
          <h4 style={{ margin: 0 }}>邀请码</h4>
          <div className="row">
            <input id="invite-count" type="number" min={1} max={10} defaultValue={1} style={{ width: 64 }} title="生成几个" />
            <input id="invite-uses" type="number" min={1} max={100} defaultValue={5} style={{ width: 64 }} title="每个可用几次" />
            <input id="invite-days" type="number" min={1} max={90} defaultValue={7} style={{ width: 64 }} title="有效天数" />
            <input id="invite-quota" type="number" min={1} max={500} defaultValue={50} style={{ width: 72 }} title="每人每天次数" />
            <button className="btn btn-sm btn-primary" onClick={() => void genInvite()}>
              生成
            </button>
          </div>
        </div>
        {!overview ? (
          <SkeletonList rows={3} />
        ) : !overview.invites.length ? (
          <p className="small faint">还没有邀请码。填好次数与天数，点「生成」。</p>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>邀请码</th>
                <th>状态</th>
                <th>已用 / 上限</th>
                <th>日额/人</th>
                <th>到期</th>
                <th>进来的人</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {overview.invites.map((i) => {
                const [label, cls] = STATE_LABEL[i.state] || ["未知", ""];
                return (
                  <tr key={i.code}>
                    <td className="mono">{i.code}</td>
                    <td>
                      <span className={`badge ${cls}`}>{label}</span>
                    </td>
                    <td className="mono tiny">
                      {i.used} / {i.max_uses}
                    </td>
                    <td>
                      <input
                        type="number"
                        min={1}
                        max={500}
                        defaultValue={i.daily_quota ?? 50}
                        style={{ width: 72, padding: "4px 8px" }}
                        onChange={async (e) => {
                          const value = Math.max(1, Math.min(500, Number(e.target.value) || 50));
                          try {
                            await api(`/api/admin/invites/${i.code}`, { method: "PATCH", body: { daily_quota: value } });
                            toast(`该码额度设为每人每天 ${value} 次`, "ok");
                          } catch (err) {
                            toast((err as Error).message, "error");
                          }
                        }}
                      />
                    </td>
                    <td className="tiny faint">{formatTime(i.expires_at)}</td>
                    <td className="mono tiny">{i.joined}</td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={async () => {
                          try {
                            await navigator.clipboard.writeText(i.code);
                            toast("已复制邀请码");
                          } catch {
                            toast(`邀请码：${i.code}`, "info", 6000);
                          }
                        }}
                      >
                        复制
                      </button>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={async () => {
                          try {
                            await api(`/api/admin/invites/${i.code}`, { method: "PATCH", body: { disabled: !i.disabled } });
                            toast(i.disabled ? "已启用" : "已停用", "ok");
                            await paint();
                          } catch (err) {
                            toast((err as Error).message, "error");
                          }
                        }}
                      >
                        {i.disabled ? "启用" : "停用"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      <section className="section">
        <div className="row-between" style={{ marginBottom: 10 }}>
          <h4 style={{ margin: 0 }}>访客</h4>
          <span className="tiny faint">额度跟邀请码走；填数字可对单人覆盖</span>
        </div>
        {visitors.length === 0 ? (
          <p className="small faint">还没有人用邀请码进来。</p>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>昵称</th>
                <th>邀请码</th>
                <th>今日次数</th>
                <th>任务</th>
                <th>单人覆盖</th>
                <th>最近活跃</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {visitors.map((v) => (
                <tr key={v.id}>
                  <td>
                    {v.nickname} {v.disabled && <span className="badge badge-warn">已停用</span>}
                  </td>
                  <td className="mono tiny">{v.invite_code}</td>
                  <td className="mono tiny">
                    {v.calls_today}
                    {v.invite_quota ? <span className="faint"> / {v.quota_override ?? v.invite_quota}</span> : null}
                  </td>
                  <td className="mono tiny">{v.tasks}</td>
                  <td>
                    <input
                      type="number"
                      min={0}
                      defaultValue={v.quota_override ?? ""}
                      placeholder={v.invite_quota ? `跟码 ${v.invite_quota}` : "跟码"}
                      style={{ width: 88, padding: "4px 8px" }}
                      onChange={async (e) => {
                        const value = e.target.value === "" ? null : Number(e.target.value);
                        try {
                          await api(`/api/admin/visitors/${v.id}`, { method: "PATCH", body: { quota_override: value } });
                          toast(value === null ? "已恢复默认额度" : `额度设为 ${value} 次/天`, "ok");
                        } catch (err) {
                          toast((err as Error).message, "error");
                        }
                      }}
                    />
                  </td>
                  <td className="tiny faint">{v.last_seen ? formatTime(v.last_seen) : "从未"}</td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    <button
                      className="btn btn-ghost btn-sm"
                      onClick={async () => {
                        try {
                          await api(`/api/admin/visitors/${v.id}`, { method: "PATCH", body: { disabled: !v.disabled } });
                          toast(!v.disabled ? "已停用该访客" : "已启用", "ok");
                          await paint();
                        } catch (err) {
                          toast((err as Error).message, "error");
                        }
                      }}
                    >
                      {v.disabled ? "启用" : "停用"}
                    </button>
                    <button
                      className="btn btn-ghost btn-sm"
                      onClick={async () => {
                        if (!(await confirmDialog("删除该访客及其全部任务、文件？此操作不可恢复。", { danger: true }))) return;
                        await api(`/api/admin/visitors/${v.id}`, { method: "DELETE" });
                        toast("已删除", "ok");
                        await paint();
                      }}
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="section">
        <h4>操作记录</h4>
        {audit.length === 0 ? (
          <p className="small faint">还没有记录。</p>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>操作者</th>
                <th>动作</th>
                <th>对象</th>
              </tr>
            </thead>
            <tbody>
              {audit.map((i, idx) => (
                <tr key={idx}>
                  <td className="tiny faint mono">{formatTime(i.created_at)}</td>
                  <td>{i.actor}</td>
                  <td className="small">{i.action}</td>
                  <td className="tiny faint mono truncate" style={{ maxWidth: 260 }}>
                    {i.target}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
