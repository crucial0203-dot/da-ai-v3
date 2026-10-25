/**
 * 共享输入区：正文 + 模型胶囊（模型/思考深度）+ 技能选择 + 计划模式 + 附件。
 * 首页与任务页共用；taskId 存在时附件直接上传到任务，否则随 send 回调带出。
 */
import { useEffect, useRef, useState } from "react";
import { ApiError, api, uploadOne } from "../lib/api";
import { formatSize } from "../lib/format";
import { shouldHandleKey } from "../lib/keyboard";
import {
  defaultModelId,
  effortLabel,
  getModel,
  toast,
  type Meta,
  type UploadRules,
} from "../store/app";

export interface ComposerParams {
  model: string;
  effort: string;
  planMode: boolean;
  skillIds: string[];
  webSearch: boolean;
  ragIds: string[];
  toolNames: string[];
}
export interface SendPayload extends ComposerParams {
  text: string;
  files: File[];
}

interface QueueItem {
  uid: string;
  name: string;
  size: number;
  file: File | null;
  state: "uploading" | "done" | "error";
  pct: number;
  error: string;
  isImage: boolean;
  visionOk: boolean;
  abort: (() => void) | null;
}

interface SkillRow {
  id: string;
  name: string;
  kind: string;
  enabled: boolean;
  use_count?: number;
  category: string;
}

interface RagCollectionRow {
  id: string;
  name: string;
  desc: string;
  doc_count: number;
  chunk_count: number;
}

interface ToolRow {
  name: string;
  label: string;
  desc: string;
  enabled: boolean;
  requires_approval: boolean;
  conversational: boolean;
}

interface Props {
  meta: Meta;
  placeholder: string;
  streaming: boolean;
  onSend: (p: SendPayload) => void;
  onStop?: () => void;
  taskId?: string;
  initial?: Partial<ComposerParams>;
  onParamsChange?: (patch: Partial<ComposerParams>) => void;
  onUploaded?: () => void;
  autoFocus?: boolean;
}

export default function Composer({
  meta,
  placeholder,
  streaming,
  onSend,
  onStop,
  taskId,
  initial,
  onParamsChange,
  onUploaded,
  autoFocus,
}: Props) {
  const [text, setText] = useState("");
  const [model, setModel] = useState(initial?.model || "");
  const [effort, setEffort] = useState(initial?.effort || "");
  const [planMode, setPlanMode] = useState(initial?.planMode || false);
  const [skillIds, setSkillIds] = useState<string[]>(initial?.skillIds || []);
  const [webSearch, setWebSearch] = useState(initial?.webSearch ?? true);
  const [ragIds, setRagIds] = useState<string[]>(initial?.ragIds || []);
  const [toolNames, setToolNames] = useState<string[]>(initial?.toolNames || []);
  const [pop, setPop] = useState<"" | "model" | "skill" | "rag" | "tool">("");
  const [skills, setSkills] = useState<SkillRow[]>([]);
  const [ragCollections, setRagCollections] = useState<RagCollectionRow[]>([]);
  const [tools, setTools] = useState<ToolRow[]>([]);
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const seqRef = useRef(0);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (autoFocus) inputRef.current?.focus();
  }, [autoFocus]);

  // 外部填充（首页示例句）：window 事件 da:fill-composer {detail: text}
  useEffect(() => {
    const fill = (e: Event) => {
      const value = (e as CustomEvent<string>).detail;
      setText(value);
      requestAnimationFrame(() => {
        const el = inputRef.current;
        if (el) {
          el.style.height = "auto";
          el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
          el.focus();
        }
      });
    };
    window.addEventListener("da:fill-composer", fill);
    return () => window.removeEventListener("da:fill-composer", fill);
  }, []);

  useEffect(() => {
    // 技能选择器只列 prompt 类且启用中的（tool/workflow 不在此分流）
    void api<{ skills: SkillRow[] }>("/api/skills")
      .then((d) => setSkills(d.skills.filter((s) => s.kind === "prompt" && s.enabled)))
      .catch(() => setSkills([]));
  }, []);

  // 知识库集合（rag.enabled 时才拉）
  useEffect(() => {
    if (!meta.rag?.enabled) return;
    void api<{ collections: RagCollectionRow[] }>("/api/rag/collections")
      .then((d) => setRagCollections(d.collections || []))
      .catch(() => setRagCollections([]));
  }, [meta.rag?.enabled]);

  // 对话可用工具（conversational + enabled）
  useEffect(() => {
    void api<{ tools: ToolRow[] }>("/api/tools")
      .then((d) => setTools((d.tools || []).filter((t) => t.conversational && t.enabled)))
      .catch(() => setTools([]));
  }, []);

  const patch = (p: Partial<ComposerParams>) => onParamsChange?.(p);

  const rules: UploadRules = meta.upload || {
    ext: [".pdf", ".docx", ".pptx", ".md", ".txt", ".csv", ".png", ".jpg", ".jpeg", ".webp"],
    image_ext: [".png", ".jpg", ".jpeg", ".webp"],
    max_mb: 20,
    max_image_mb: 8,
    max_files_per_batch: 5,
    max_images_per_task: 4,
  };
  const fileExt = (name: string) => {
    const i = name.lastIndexOf(".");
    return i >= 0 ? name.slice(i).toLowerCase() : "";
  };
  const isImageExt = (name: string) => rules.image_ext.includes(fileExt(name));

  function autoGrow(el: HTMLTextAreaElement) {
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }

  async function addFiles(fileList: FileList | File[] | null) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    const accepted: File[] = [];
    for (const file of files) {
      const ext = fileExt(file.name);
      if (!rules.ext.includes(ext)) {
        toast(`${file.name}：类型不支持`, "error");
        continue;
      }
      const limit = (isImageExt(file.name) ? rules.max_image_mb : rules.max_mb) * 1024 * 1024;
      if (file.size > limit) {
        toast(`${file.name}：超过 ${Math.round(limit / 1024 / 1024)}MB`, "error");
        continue;
      }
      accepted.push(file);
    }
    if (!accepted.length) return;
    if (accepted.length > rules.max_files_per_batch) {
      toast(`一次最多 ${rules.max_files_per_batch} 个文件，已截取`, "error");
      accepted.length = rules.max_files_per_batch;
    }

    const batch: QueueItem[] = accepted.map((file) => ({
      uid: `a${++seqRef.current}`,
      name: file.name,
      size: file.size,
      file,
      state: "uploading",
      pct: 0,
      error: "",
      isImage: isImageExt(file.name),
      visionOk: true,
      abort: null,
    }));
    setQueue((q) => [...q, ...batch]);

    for (const item of batch) {
      if (!taskId) continue; // 首页模式：文件留在队列里，发送时一起带出
      item.state = "uploading";
      item.pct = 0;
      const { promise, abort } = uploadOne(taskId, item.file!, (pct) => {
        item.pct = pct;
        setQueue((q) => [...q]);
      });
      item.abort = abort;
      try {
        const result = await promise;
        const row = (result.files || [])[0] || {};
        const m = row.meta || {};
        item.isImage = Boolean(m.image);
        if (item.isImage) {
          item.state = "done";
          item.visionOk = row.vision_usable !== false;
        } else if (m.has_text === false) {
          item.state = "error";
          item.error = (m.note as string) || "没有可用文字";
        } else {
          item.state = "done";
        }
      } catch (err) {
        item.state = "error";
        item.error = err instanceof ApiError ? err.message : "上传失败";
      } finally {
        item.abort = null;
        setQueue((q) => [...q]);
      }
    }
    if (taskId) onUploaded?.();
    // 成功的条目停留一会儿再收走
    setTimeout(() => {
      setQueue((q) => q.filter((x) => !(batch.some((b) => b.uid === x.uid) && x.state === "done")));
    }, 1800);
  }

  function doSend() {
    const value = text.trim();
    if (!value || streaming) return;
    const pendingFiles = taskId ? [] : queue.filter((q) => q.state !== "error").map((q) => q.file!).filter(Boolean);
    const errored = queue.some((q) => q.state === "error");
    if (errored) {
      toast("有附件没传成功，先处理或移除它", "error");
      return;
    }
    setText("");
    if (inputRef.current) autoGrow(inputRef.current);
    setQueue([]);
    onSend({ text: value, model, effort, planMode, skillIds, webSearch, ragIds, toolNames, files: pendingFiles });
  }

  const spec = getModel(meta, model) || getModel(meta, defaultModelId(meta)) || meta.models[0];
  const modelName = spec ? spec.short || spec.label : "默认模型";
  const effName = effortLabel(meta, effort) || "默认深度";
  const pickedSkills = skillIds.map((id) => skills.find((s) => s.id === id)).filter(Boolean) as SkillRow[];

  return (
    <div className="composer" data-testid="composer">
      {queue.length > 0 && (
        <div className="composer-inner" style={{ marginBottom: 8, padding: 8 }}>
          <div className="attach-tray">
            {queue.map((item) => (
              <div key={item.uid} className="attach-chip" data-state={item.state}>
                <span className="chip-text grow">
                  <span className="chip-name truncate" title={item.name}>
                    {item.name}
                  </span>
                  <span className="chip-sub">
                    {item.state === "uploading"
                      ? `${item.pct}%`
                      : item.state === "error"
                        ? item.error
                        : item.isImage
                          ? `图片 · ${item.visionOk ? "当前模型可读" : "需切多模态模型"}`
                          : `${formatSize(item.size)} · 已加入`}
                  </span>
                </span>
                {item.state === "error" && item.file && (
                  <button className="btn btn-ghost btn-xs" onClick={() => void addFiles([item.file!])}>
                    重试
                  </button>
                )}
                <button
                  className="btn btn-ghost btn-xs"
                  aria-label="移除"
                  onClick={() => {
                    item.abort?.();
                    setQueue((q) => q.filter((x) => x.uid !== item.uid));
                  }}
                >
                  ×
                </button>
                {item.state === "uploading" && (
                  <span className="chip-bar">
                    <i style={{ width: `${item.pct}%` }} />
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="composer-inner">
        <textarea
          ref={inputRef}
          data-testid="composer-input"
          rows={1}
          placeholder={placeholder}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            autoGrow(e.target);
          }}
          onKeyDown={(e) => {
            // 输入法组合期（候选词上屏）也会派发回车，那不是让用户"发送"
            if (e.key === "Enter" && !e.shiftKey && shouldHandleKey(e)) {
              e.preventDefault();
              doSend();
            }
          }}
        />
        <div className="composer-bar">
          <button
            type="button"
            className={`model-pill ${model || effort ? "custom" : ""}`}
            data-testid="model-pill"
            onClick={(e) => {
              e.stopPropagation();
              setPop(pop === "model" ? "" : "model");
            }}
            title="切换模型与思考深度"
          >
            {modelName}
            <span className="pill-sep">·</span>
            {effName}
          </button>

          {skills.length > 0 && (
            <button
              type="button"
              className={`plan-toggle ${skillIds.length ? "on" : ""}`}
              data-testid="skill-toggle"
              onClick={(e) => {
                e.stopPropagation();
                setPop(pop === "skill" ? "" : "skill");
              }}
              title="选择写作技能"
            >
              技能{skillIds.length ? ` ${skillIds.length}` : ""}
            </button>
          )}

          <button
            type="button"
            className={`plan-toggle ${planMode ? "on" : ""}`}
            data-testid="plan-toggle"
            onClick={(e) => {
              e.stopPropagation();
              const next = !planMode;
              setPlanMode(next);
              patch({ planMode: next });
            }}
            title="计划模式：先出执行计划，确认后再动手"
          >
            计划
          </button>

          {meta.web?.enabled && (
            <button
              type="button"
              className={`plan-toggle ${webSearch ? "on" : ""}`}
              data-testid="web-toggle"
              onClick={(e) => {
                e.stopPropagation();
                const next = !webSearch;
                setWebSearch(next);
                patch({ webSearch: next });
              }}
              title={
                meta.web.providers?.some((p) => p.degraded)
                  ? `联网搜索（部分源降级：${meta.web.providers.filter((p) => p.degraded).map((p) => p.label).join("、")}）`
                  : "联网搜索：回答前先检索网页"
              }
            >
              联网
            </button>
          )}

          {meta.rag?.enabled && ragCollections.length > 0 && (
            <button
              type="button"
              className={`plan-toggle ${ragIds.length ? "on" : ""}`}
              data-testid="rag-collection"
              onClick={(e) => {
                e.stopPropagation();
                setPop(pop === "rag" ? "" : "rag");
              }}
              title="从知识库检索相关内容注入回答"
            >
              知识库{ragIds.length ? ` ${ragIds.length}` : ""}
            </button>
          )}

          {tools.length > 0 && (
            <button
              type="button"
              className={`plan-toggle ${toolNames.length ? "on" : ""}`}
              data-testid="tool-picker"
              onClick={(e) => {
                e.stopPropagation();
                setPop(pop === "tool" ? "" : "tool");
              }}
              title="允许对话中调用的工具"
            >
              工具{toolNames.length ? ` ${toolNames.length}` : ""}
            </button>
          )}

          <span className="composer-spacer" />

          <button
            type="button"
            className="btn btn-ghost btn-icon"
            aria-label="添加材料"
            title="添加材料"
            onClick={() => fileRef.current?.click()}
          >
            📎
          </button>
          <input
            ref={fileRef}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              void addFiles(e.target.files);
              e.target.value = "";
            }}
          />

          {streaming ? (
            <button
              className="btn btn-sm"
              data-testid="stop-btn"
              title="停止生成，已写出的部分会保留，可以接着写"
              onClick={() => onStop?.()}
            >
              停止
            </button>
          ) : (
            <button className="btn btn-primary btn-sm" data-testid="send-btn" onClick={doSend}>
              发送
            </button>
          )}
        </div>

        {pop === "model" && (
          <div className="model-pop" data-testid="model-pop" onClick={(e) => e.stopPropagation()}>
            <div className="model-pop-title">模型</div>
            {meta.models.map((m) => (
              <button
                key={m.id}
                className={`model-opt ${(model || defaultModelId(meta)) === m.id ? "active" : ""}`}
                onClick={() => {
                  setModel(m.id === defaultModelId(meta) ? "" : m.id);
                  patch({ model: m.id });
                  setPop("");
                  toast(`下一条消息起用「${m.short || m.label}」`);
                }}
              >
                <span className="model-opt-main">
                  {m.short || m.label}
                  {m.vision && <span className="badge badge-ok">看图</span>}
                  {m.id === defaultModelId(meta) && <span className="model-opt-def">默认</span>}
                </span>
                <span className="model-opt-note">{m.note}</span>
              </button>
            ))}
            <div className="model-pop-title">思考深度</div>
            <div className="effort-row">
              {["", ...meta.efforts].map((key) => (
                <button
                  key={key || "auto"}
                  className={`effort-opt ${(effort || "") === key ? "active" : ""}`}
                  onClick={() => {
                    setEffort(key);
                    patch({ effort: key });
                  }}
                >
                  {key ? effortLabel(meta, key) : "跟偏好"}
                </button>
              ))}
            </div>
          </div>
        )}

        {pop === "skill" && (
          <div className="skill-pop" data-testid="skill-pop" onClick={(e) => e.stopPropagation()}>
            <div className="model-pop-title">写作技能（最多 5 个，只注入写法）</div>
            {skills.map((s) => {
              const on = skillIds.includes(s.id);
              return (
                <button
                  key={s.id}
                  className="skill-opt"
                  onClick={() => {
                    let next: string[];
                    if (on) next = skillIds.filter((x) => x !== s.id);
                    else if (skillIds.length >= 5) {
                      toast("最多绑定 5 个技能", "error");
                      return;
                    } else next = [...skillIds, s.id];
                    setSkillIds(next);
                    patch({ skillIds: next });
                  }}
                >
                  <span className="tick">{on ? "✓" : ""}</span>
                  <span className="grow">
                    <span style={{ fontWeight: 600, fontSize: 13 }}>{s.name}</span>
                    <span className="chip-sub">
                      {s.category}
                      {typeof s.use_count === "number" ? ` · 已用 ${s.use_count} 次` : ""}
                    </span>
                  </span>
                </button>
              );
            })}
            {pickedSkills.length > 0 && (
              <p className="tiny faint" style={{ margin: "6px 8px 2px" }}>
                已选：{pickedSkills.map((s) => s.name).join("、")}
              </p>
            )}
          </div>
        )}

        {pop === "rag" && (
          <div className="skill-pop" data-testid="rag-pop" onClick={(e) => e.stopPropagation()}>
            <div className="model-pop-title">知识库集合（可多选，检索片段注入上下文）</div>
            {ragCollections.map((c) => {
              const on = ragIds.includes(c.id);
              return (
                <button
                  key={c.id}
                  className="skill-opt"
                  onClick={() => {
                    const next = on ? ragIds.filter((x) => x !== c.id) : [...ragIds, c.id];
                    setRagIds(next);
                    patch({ ragIds: next });
                  }}
                >
                  <span className="tick">{on ? "✓" : ""}</span>
                  <span className="grow">
                    <span style={{ fontWeight: 600, fontSize: 13 }}>{c.name}</span>
                    <span className="chip-sub">
                      {c.doc_count} 篇 · {c.chunk_count} 块
                      {c.desc ? ` · ${c.desc}` : ""}
                    </span>
                  </span>
                </button>
              );
            })}
            {ragCollections.length === 0 && (
              <p className="tiny faint" style={{ margin: "6px 8px 2px" }}>
                还没有集合，去「知识库」页建一个并添加文档。
              </p>
            )}
          </div>
        )}

        {pop === "tool" && (
          <div className="skill-pop" data-testid="tool-pop" onClick={(e) => e.stopPropagation()}>
            <div className="model-pop-title">可用工具（模型判断需要时自动调用）</div>
            {tools.map((t) => {
              const on = toolNames.includes(t.name);
              return (
                <button
                  key={t.name}
                  className="skill-opt"
                  onClick={() => {
                    const next = on ? toolNames.filter((x) => x !== t.name) : [...toolNames, t.name];
                    setToolNames(next);
                    patch({ toolNames: next });
                  }}
                >
                  <span className="tick">{on ? "✓" : ""}</span>
                  <span className="grow">
                    <span style={{ fontWeight: 600, fontSize: 13 }}>
                      {t.label || t.name}
                      {t.requires_approval && <span className="badge badge-warn">需审批</span>}
                    </span>
                    <span className="chip-sub">{t.desc}</span>
                  </span>
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* 点击空白收起浮层 */}
      {pop !== "" && <div style={{ position: "fixed", inset: 0, zIndex: 5 }} onClick={() => setPop("")} />}
    </div>
  );
}
