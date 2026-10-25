/**
 * 图结构撤销/重做（P2-4）：快照上限 50 步，mod+z 撤销 / mod+shift+z 重做。
 *
 * - 快照 = 整个图状态（节点+边，由调用方给 current 值）；
 * - 连续变更（拖动、逐字输入）用 300ms 防抖合并成一步；
 * - 焦点在 input/textarea/select 或 contentEditable 时不拦截按键，
 *   否则打字打到 mod+z（如「MZ」组合）会误触发撤销。
 */
import { useEffect, useRef } from "react";
import { shouldHandleKey } from "../lib/keyboard";

const MAX_STEPS = 50;
const DEBOUNCE_MS = 300;

export function useUndo<T>(current: T, apply: (snapshot: T) => void): void {
  const undoStack = useRef<T[]>([]);
  const redoStack = useRef<T[]>([]);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** apply 引用走 ref，避免把它放进 effect 依赖导致重挂监听 */
  const applyRef = useRef(apply);
  applyRef.current = apply;
  const curRef = useRef(current);
  curRef.current = current;

  useEffect(() => {
    const same = (a: T | undefined, b: T) => JSON.stringify(a) === JSON.stringify(b);
    const push = () => {
      const top = undoStack.current[undoStack.current.length - 1];
      if (same(top, curRef.current)) return;
      undoStack.current.push(curRef.current);
      if (undoStack.current.length > MAX_STEPS) undoStack.current.shift();
      redoStack.current = [];
    };

    // 订阅 current 变化：任何引用变化先清防抖再计时
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(push, DEBOUNCE_MS);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current]);

  useEffect(() => {
    function inEditable(el: EventTarget | null): boolean {
      if (!(el instanceof HTMLElement)) return false;
      return (
        el.matches("input, textarea, select") || el.isContentEditable
      );
    }

    function onKeyDown(e: KeyboardEvent) {
      const mod = e.metaKey || e.ctrlKey;
      if (!mod || e.key.toLowerCase() !== "z" || inEditable(e.target)) return;
      // 输入法组合期的字母也会被当作快捷键的一部分
      if (!shouldHandleKey(e)) return;
      e.preventDefault();
      if (e.shiftKey) {
        // 重做：撤销栈不动，把 redo 顶回当前
        const next = redoStack.current.pop();
        if (next === undefined) return;
        undoStack.current.push(next);
        applyRef.current(next);
      } else {
        // 撤销：栈里至少要有「上一步 + 当前态」两帧才有得退
        if (undoStack.current.length < 2) return;
        const top = undoStack.current.pop() as T;
        redoStack.current.push(top);
        applyRef.current(undoStack.current[undoStack.current.length - 1]);
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
}
