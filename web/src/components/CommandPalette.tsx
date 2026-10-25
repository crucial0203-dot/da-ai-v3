/**
 * 全局命令面板（P2-7）：mod+k 呼出，三类 section（导航/任务/操作），
 * 每类最多 80 条。手写 ~150 行不引 cmdk——但类名沿用 cmdk-* 约定，
 * 之后想换真 cmdk 时样式基本不用改。
 *
 * item 的 value 必须稳定唯一（nav:/skills / task:<id> / act:new-task），
 * 选中态与测试断言都靠它锚定。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { useSession } from "../store/app";
import { shouldHandleKey } from "../lib/keyboard";

interface PaletteItem {
  /** 稳定唯一 key */
  value: string;
  label: string;
  hint?: string;
  run: () => void;
}

interface Section {
  heading: string;
  items: PaletteItem[];
}

const MAX_PER_SECTION = 80;

export default function CommandPalette() {
  const open = useSession((s) => s.paletteOpen);
  const setPaletteOpen = useSession((s) => s.setPaletteOpen);
  const tasks = useSession((s) => s.tasks);
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  const close = () => setPaletteOpen(false);

  useEffect(() => {
    if (open) {
      setQ("");
      setActive(0);
      // 等挂载完再聚焦
      setTimeout(() => inputRef.current?.focus(), 30);
    }
  }, [open]);

  // Esc 关闭走窗口级监听：刚打开时 input 可能还没聚焦完（30ms 延迟），
  // 只挂 input 的 onKeyDown 会漏掉这一瞬间的 Esc。
  useEffect(() => {
    if (!open) return;
    function onWinKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        setPaletteOpen(false);
      }
    }
    window.addEventListener("keydown", onWinKey);
    return () => window.removeEventListener("keydown", onWinKey);
  }, [open, setPaletteOpen]);

  const sections = useMemo<Section[]>(() => {
    const nav: PaletteItem[] = [
      ["/", "首页"],
      ["/skills", "技能"],
      ["/library", "资料库"],
      ["/kb", "知识库"],
      ["/tools", "工具"],
      ["/memory", "记忆"],
      ["/plugins", "插件市场"],
      ["/canvas", "画布"],
      ["/settings", "设置"],
    ].map(([path, label]) => ({
      value: `nav:${path}`,
      label,
      hint: path,
      run: () => {
        close();
        void navigate(path);
      },
    }));

    const taskItems: PaletteItem[] = tasks.slice(0, MAX_PER_SECTION).map((t) => ({
      value: `task:${t.id}`,
      label: t.title || "未命名任务",
      hint: "任务",
      run: () => {
        close();
        void navigate(`/t/${t.id}`);
      },
    }));

    const actions: PaletteItem[] = [
      {
        value: "act:new-chat",
        label: "新建对话",
        run: () => {
          close();
          void navigate("/");
        },
      },
      {
        value: "act:new-workflow",
        label: "新建工作流",
        run: () => {
          close();
          void navigate("/canvas");
        },
      },
    ].slice(0, MAX_PER_SECTION);

    const kw = q.trim().toLowerCase();
    const hit = (item: PaletteItem) =>
      !kw || item.label.toLowerCase().includes(kw) || item.value.toLowerCase().includes(kw);
    return [
      { heading: "操作", items: actions.filter(hit) },
      { heading: "导航", items: nav.filter(hit) },
      { heading: "最近任务", items: taskItems.filter(hit) },
    ].filter((s) => s.items.length > 0);
  }, [q, tasks, navigate]);

  const flat = useMemo(() => sections.flatMap((s) => s.items), [sections]);

  useEffect(() => {
    setActive(0);
  }, [q]);

  if (!open) return null;

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(i + 1, flat.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      if (!shouldHandleKey(e)) return;
      e.preventDefault();
      flat[active]?.run();
    }
  };

  let idx = -1;
  return (
    <div className="cmdk-veil" data-testid="cmdk-veil" onMouseDown={close}>
      <div className="cmdk" data-testid="command-palette" role="dialog" onMouseDown={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          className="cmdk-input"
          data-testid="cmdk-input"
          placeholder="搜索页面、任务、操作…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKey}
        />
        <div className="cmdk-list" data-testid="cmdk-list">
          {flat.length === 0 && <p className="cmdk-empty">没有匹配项</p>}
          {sections.map((s) => (
            <div key={s.heading} className="cmdk-section">
              <div className="cmdk-heading">{s.heading}</div>
              {s.items.map((item) => {
                idx += 1;
                const i = idx;
                return (
                  <button
                    key={item.value}
                    className={`cmdk-item ${i === active ? "on" : ""}`}
                    data-testid="cmdk-item"
                    data-value={item.value}
                    onMouseEnter={() => setActive(i)}
                    onClick={item.run}
                  >
                    <span className="cmdk-item-label">{item.label}</span>
                    {item.hint && <span className="cmdk-item-hint">{item.hint}</span>}
                  </button>
                );
              })}
            </div>
          ))}
        </div>
        <div className="cmdk-foot">
          <span>↑↓ 选择</span>
          <span>↵ 打开</span>
          <span>esc 关闭</span>
        </div>
      </div>
    </div>
  );
}
