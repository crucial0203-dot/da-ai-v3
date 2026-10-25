/** 进入页：邀请码 + 昵称（访客）/ 独立密码（管理员 /admin 路径）。科技感落地页版式。 */
import { useState } from "react";
import { ApiError, api } from "../lib/api";
import { useSession } from "../store/app";

export default function Auth({ isAdminPath, onDone }: { isAdminPath: boolean; onDone: () => void }) {
  const boot = useSession((s) => s.boot);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError("");
    const form = e.currentTarget;
    if (isAdminPath) {
      setBusy(true);
      try {
        await api("/api/admin/login", { method: "POST", body: { password: form.password.value } });
        await boot();
        onDone();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "网络异常，请重试");
      } finally {
        setBusy(false);
      }
      return;
    }
    const code = form.code.value.trim().toUpperCase();
    const nickname = form.nickname.value.trim();
    if (code.length < 4) return setError("请填完整的邀请码");
    if (nickname.length < 2) return setError("昵称至少 2 个字");
    setBusy(true);
    try {
      await api("/api/join", { method: "POST", body: { code, nickname } });
      await boot();
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "网络异常，请重试");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-wrap">
      {/* 左：品牌宣言 */}
      <aside className="auth-side">
        <div className="auth-deco auth-grid" aria-hidden />
        <div className="auth-deco auth-orb auth-orb-a" aria-hidden />
        <div className="auth-deco auth-orb auth-orb-b" aria-hidden />
        <div>
          <div className="brand" style={{ marginBottom: 40 }}>
            <span className="brand-mark" />
            <span>
              <span className="brand-name">DA的AI</span>
              <span className="brand-sub">WORK DELIVERY PLATFORM</span>
            </span>
          </div>
          <h1>
            把活派出去，
            <br />
            拿走成品文件。
          </h1>
        </div>
        <div>
          <div className="quote">不是聊天记录，是交付物。</div>
          <div className="auth-sign">DA的AI · 工作交付平台</div>
        </div>
      </aside>

      {/* 右：进入表单 */}
      <section className="auth-form">
        <div className="auth-form-inner">
          <div className="auth-eyebrow">INVITE ACCESS</div>
          <h2>{isAdminPath ? "管理员登录" : "用邀请码进入"}</h2>
          {!isAdminPath && <p className="auth-form-sub">按次数发放 · 可设有效期</p>}
          <form onSubmit={submit} noValidate data-testid="auth-form">
            {isAdminPath ? (
              <div className="field">
                <label htmlFor="password">密码</label>
                <input id="password" name="password" type="password" autoComplete="current-password" placeholder="管理员密码" />
              </div>
            ) : (
              <>
                <div className="field">
                  <label htmlFor="code">邀请码</label>
                  <input
                    id="code"
                    name="code"
                    type="text"
                    autoComplete="off"
                    placeholder="8 位字母数字"
                    autoCapitalize="characters"
                    spellCheck={false}
                  />
                </div>
                <div className="field">
                  <label htmlFor="nickname">昵称</label>
                  <input id="nickname" name="nickname" type="text" autoComplete="nickname" placeholder="2–20 字" />
                  <div className="hint">只用来在这台设备上认你，不用密码，也不用记住</div>
                </div>
              </>
            )}
            {error && (
              <div className="form-error" data-testid="auth-error">
                {error}
              </div>
            )}
            <button className="auth-submit" type="submit" disabled={busy} data-testid="auth-submit">
              {busy ? <span className="spinner" /> : null} {isAdminPath ? "登录" : "进入工作台"}
            </button>
          </form>
          <details className="auth-faq">
            <summary>什么是邀请码？</summary>
            <p>邀请码由管理员发放，一个码可用几次、多久有效由发放时决定。</p>
          </details>
          <p className="auth-form-foot">
            {isAdminPath ? (
              <a href="/">回到访客入口</a>
            ) : (
              <a href="/admin">管理员入口</a>
            )}
          </p>
        </div>
      </section>
    </div>
  );
}
