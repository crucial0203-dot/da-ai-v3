/**
 * 首页 = 对话（V3.2 决策「对话即首页」）。
 * 发送即建任务：创建任务（带模式/技能/计划）→ 传附件 → 跳任务页自动派发首条消息。
 * 空态给欢迎语 + 模式卡；非空态下方列最近会话。
 */
import { useState } from "react";
import { useNavigate } from "react-router";
import Composer, { type SendPayload } from "../components/Composer";
import { ApiError, api, uploadOne } from "../lib/api";
import { formatTime } from "../lib/format";
import { setPendingDispatch, toast, useSession } from "../store/app";
import { Slot } from "../plugins/slots";

const STARTERS: Record<string, string[]> = {
  chat: ["帮我理一下这个思路的漏洞", "用大白话解释一下这个概念", "给我三个角度的看法，各两句话"],
  research: ["拆解这份材料的论证结构，标出最弱的一步", "找出争议焦点，并说明双方的依据", "归纳适用条件，逐条对应材料页码"],
  writing: ["写一篇 1500 字读书笔记，宋体五号排版", "把这段草稿改成学术书面语，保留原意", "列一份文献综述提纲，标注可追问的问题"],
  slides: ["做 4 页学术汇报，标题即结论", "把这份材料压成 6 页，每页 90 字以内", "给这 4 页各写一段 80 字讲稿"],
  docs: ["整理成一份课程纪要，要素齐全", "起草一份情况说明，语气克制", "把要点排成正式报告，带章节编号"],
};

export default function Home() {
  const { meta, tasks, refreshTasks } = useSession();
  const navigate = useNavigate();
  const [mode, setMode] = useState("chat");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(0); // 有过派发后显示最近会话

  async function handleSend(p: SendPayload) {
    setBusy(true);
    try {
      const data = await api<{ task: { id: string } }>("/api/tasks", {
        method: "POST",
        body: {
          mode,
          goal: p.text,
          ...(p.skillIds.length ? { prompt_skill_ids: p.skillIds } : {}),
          params: {
            web_search: p.webSearch ? 1 : 0,
            ...(p.ragIds.length ? { rag_collection_ids: p.ragIds } : {}),
            ...(p.toolNames.length ? { tool_names: p.toolNames } : {}),
          },
        },
      });
      const taskId = data.task.id;

      // 首页带的附件：先落到任务上，失败不拦发送
      for (const file of p.files) {
        try {
          await uploadOne(taskId, file).promise;
        } catch (err) {
          toast(`${file.name}：${err instanceof ApiError ? err.message : "上传失败"}`, "error");
        }
      }

      setPendingDispatch({
        taskId,
        text: p.text,
        model: p.model,
        effort: p.effort,
        planMode: p.planMode,
        skillIds: p.skillIds,
        webSearch: p.webSearch,
        ragIds: p.ragIds,
        toolNames: p.toolNames,
      });
      void refreshTasks();
      setSent((n) => n + 1);
      navigate(`/t/${taskId}`);
    } catch (err) {
      toast(err instanceof ApiError ? err.message : "创建失败", "error");
      setBusy(false);
    }
  }

  const modes = meta?.modes || [];
  const labelOf = (key: string) => modes.find((m) => m.key === key)?.label || key;

  return (
    <div style={{ overflowY: "auto", flex: 1 }} className="page">
      <div className="home-hero">
        <h1>{meta?.app || "DA的AI"}</h1>
        <p>文档、演示、检索、对话，从同一个入口进去。交付的是文件，不是聊天记录。</p>
      </div>

      <div style={{ maxWidth: 720, margin: "0 auto", width: "100%" }}>
        <div className="row wrap" style={{ justifyContent: "center", marginBottom: 12, gap: 8 }} data-testid="mode-chips">
          {modes.map((m) => (
            <button key={m.key} className={`mode-chip ${mode === m.key ? "active" : ""}`} onClick={() => setMode(m.key)}>
              <strong>{m.label}</strong>
              <span>{m.desc}</span>
            </button>
          ))}
        </div>

        <Composer
          meta={meta!}
          placeholder={busy ? "正在创建会话…" : `写一句要交的东西（${labelOf(mode)}）`}
          streaming={busy}
          onSend={handleSend}
          autoFocus
        />

        <div className="row wrap" style={{ justifyContent: "center", marginTop: 10, gap: 6 }}>
          {(STARTERS[mode] || []).map((text) => (
            <button
              key={text}
              className="starter"
              onClick={() => window.dispatchEvent(new CustomEvent("da:fill-composer", { detail: text }))}
            >
              {text}
            </button>
          ))}
        </div>

        {/* 插件挂载点：home.afterComposer */}
        <Slot name="home.afterComposer" />
      </div>

      {(sent > 0 || tasks.length > 0) && (
        <div style={{ maxWidth: 720, margin: "40px auto 0", width: "100%" }} data-testid="recent-tasks">
          <div className="row-between" style={{ marginBottom: 10 }}>
            <h3 style={{ margin: 0, fontSize: 16 }}>最近会话</h3>
            <button className="btn btn-ghost btn-sm" onClick={() => void refreshTasks()}>
              刷新
            </button>
          </div>
          {tasks.length === 0 ? (
            <div className="empty">
              <h4>还没有会话</h4>
              <p>写一句要交的东西，直接发送。</p>
            </div>
          ) : (
            tasks.map((t) => (
              <article key={t.id} className="task-item" onClick={() => navigate(`/t/${t.id}`)} data-testid="task-item">
                <div className="grow">
                  <h4>{t.title}</h4>
                  <div className="task-meta">
                    <span>{labelOf(t.mode)}</span>
                    <span>·</span>
                    <span>{formatTime(t.updated_at)}</span>
                    {t.mode !== "chat" && (
                      <>
                        <span>·</span>
                        <span>
                          {t.nodes_done}/{t.nodes_total} 阶段
                        </span>
                      </>
                    )}
                    {t.nodes_failed > 0 && <span className="badge badge-warn">{t.nodes_failed} 处待重试</span>}
                  </div>
                </div>
                <span className={`badge ${t.status === "delivered" ? "badge-ok" : ""}`}>
                  {t.status === "delivered" ? "已交付" : t.status === "running" ? "执行中" : t.nodes_failed ? "待处理" : t.nodes_done ? "进行中" : "新任务"}
                </span>
              </article>
            ))
          )}
        </div>
      )}
    </div>
  );
}
