/** 设置：账号、模型偏好、用量图表、能力边界。 */
import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api } from "../lib/api";
import { SkeletonList } from "../components/Skeleton";
import { formatNumber } from "../lib/format";
import { confirmDialog, toast, useSession } from "../store/app";
import { Slot } from "../plugins/slots";

interface Usage {
  days: Array<{ day: string; in_tokens: number; out_tokens: number; cache_read: number }>;
  today: { in_tokens: number; out_tokens: number; cache_read: number };
  quota: { used: number; quota: number };
}

export default function Settings() {
  const { meta, me, reloadMe, logout, setTheme, theme } = useSession();
  const [nickname, setNickname] = useState(me?.nickname || "");
  const [usage, setUsage] = useState<Usage | null>(null);
  const [usageLoading, setUsageLoading] = useState(true);

  useEffect(() => {
    void api<Usage>("/api/usage")
      .then(setUsage)
      .catch(() => setUsage(null))
      .finally(() => setUsageLoading(false));
  }, []);

  if (!me || !meta) return null;

  async function patchMe(body: Record<string, unknown>, okText: string) {
    try {
      await api("/api/me", { method: "PATCH", body });
      await reloadMe();
      toast(okText, "ok");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  const maxTokens = Math.max(1, ...(usage?.days || []).map((d) => d.in_tokens + d.out_tokens));

  return (
    <div className="page page-narrow">
      <div className="page-head">
        <div>
          <h1>设置</h1>
          <div className="page-sub">
            {me.nickname} · {me.role === "admin" ? "管理员" : "普通用户"}
          </div>
        </div>
        {me.role === "admin" && (
          <Link className="btn" to="/admin">
            管理台
          </Link>
        )}
      </div>

      <section className="section">
        <h4>账号</h4>
        <div className="form-row">
          <div>
            <span className="small">昵称</span>
            <div className="hint">用于登录与显示，2–20 字</div>
          </div>
          <div className="row">
            <input value={nickname} onChange={(e) => setNickname(e.target.value)} maxLength={20} style={{ width: 160 }} />
            <button className="btn" onClick={() => void patchMe({ nickname: nickname.trim() }, "已保存")}>
              保存
            </button>
          </div>
        </div>
        <div className="form-row">
          <div>
            <span className="small">外观</span>
            <div className="hint">浅色 / 深色 / 纯黑</div>
          </div>
          <div className="row">
            {(["light", "dark", "oled"] as const).map((t) => (
              <button key={t} className={`filter-chip ${theme === t ? "active" : ""}`} onClick={() => setTheme(t)}>
                {t === "light" ? "浅色" : t === "dark" ? "深色" : "纯黑"}
              </button>
            ))}
          </div>
        </div>
        <div className="form-row">
          <div>
            <span className="small">退出登录</span>
            <div className="hint">只影响当前浏览器</div>
          </div>
          <div>
            <button
              className="btn btn-danger"
              onClick={async () => {
                if (!(await confirmDialog("退出登录？", { okLabel: "退出" }))) return;
                await logout();
                location.assign("/");
              }}
            >
              退出
            </button>
          </div>
        </div>
      </section>

      <section className="section">
        <h4>模型</h4>
        <div className="form-row">
          <div>
            <span className="small">默认模型</span>
            <div className="hint">新建会话时用它；会话里可随时切换</div>
          </div>
          <div>
            <select
              value={me.pref_model || meta.default_model}
              onChange={(e) => void patchMe({ pref_model: e.target.value }, "已更新默认模型")}
            >
              {meta.models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                  {m.vision ? " · 可看图" : ""}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="form-row">
          <div>
            <span className="small">思考深度</span>
            <div className="hint">越深越慢，也越贵</div>
          </div>
          <div>
            <select value={me.pref_effort || ""} onChange={(e) => void patchMe({ pref_effort: e.target.value }, "已更新思考深度")}>
              {meta.efforts.map((k) => (
                <option key={k} value={k}>
                  {meta.effort_labels[k] || k}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="form-row">
          <div>
            <span className="small">保存思考过程</span>
            <div className="hint">多轮续写更省钱；关掉则只留正文</div>
          </div>
          <div>
            <button
              className={`switch ${me.pref_keep_thinking ? "on" : ""}`}
              onClick={() => void patchMe({ pref_keep_thinking: !me.pref_keep_thinking }, "已更新")}
            />
          </div>
        </div>
      </section>

      <section className="section">
        <h4>用量</h4>
        {usageLoading ? (
          <SkeletonList rows={3} />
        ) : !usage ? (
          <p className="small faint">用量暂时读不到</p>
        ) : (
          <>
            <div className="row-between small" style={{ marginBottom: 12 }}>
              <span>
                今日 {formatNumber(usage.today.in_tokens + usage.today.out_tokens)} tokens · 额度 {usage.quota.used}/{usage.quota.quota} 次
              </span>
              <span className="faint tiny">缓存命中 {formatNumber(usage.today.cache_read)}</span>
            </div>
            <div className="bars">
              {(usage.days || []).map((d) => {
                const total = d.in_tokens + d.out_tokens;
                const h = Math.max(3, Math.round((total / maxTokens) * 72));
                return (
                  <div key={d.day} style={{ flex: 1 }}>
                    <div className="bar" style={{ height: h }} title={`${d.day}：${total} tokens，缓存命中 ${d.cache_read}`} />
                    <div className="bar-label">{d.day.slice(5).replace("-", "/")}</div>
                  </div>
                );
              })}
              {!usage.days?.length && <span className="tiny faint">近 14 天没有记录</span>}
            </div>
          </>
        )}
      </section>

      <section className="section">
        <h4>能力边界</h4>
        <div className="card small" style={{ lineHeight: 1.9 }}>
          <ul style={{ marginBottom: 0, paddingLeft: "1.2em" }}>
            <li>扫描版 PDF 没有文字层时读不出内容，需要你先做 OCR。</li>
            <li>材料是整段进上下文的，太长会被截断，优先给最相关的几份。</li>
            <li>Word 的自动目录是域，需要在 Word 里按 F9 更新；脚注不自动生成。</li>
            <li>PPT 走六节点流程：内容写完先给你确认大纲，确认后渲染不再扣次数；提出修改会重写内容并计 1 次。</li>
            <li>上传的脚本技能永不执行；技能正文只影响写法，不新增工具能力。</li>
            <li>模型不会替你在本地改文件、跑代码或访问你自己的电脑。</li>
          </ul>
        </div>
      </section>

      {/* 插件挂载点：settings.plugins */}
      <Slot name="settings.plugins" />
    </div>
  );
}
