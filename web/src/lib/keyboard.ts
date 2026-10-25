/**
 * 键盘事件的公共闸门。
 *
 * 中文输入法在这件事上骗人：候选词上屏时浏览器**同时**派发 keydown(Enter)，
 * 用户只是想把拼音变成字，代码却把它当成"按下发送/执行"。
 * 所以凡是"回车或快捷键等于提交"的地方，都先过一遍 shouldHandleKey。
 *
 * 判断统一用一个端口：`isComposing` / `isComposing===true` 在 CompositionEvent
 * 期间为真；部分输入法会给 keyCode 229，两者取或。
 */
export function shouldHandleKey(e: KeyboardEvent | React.KeyboardEvent): boolean {
  const native = (e as React.KeyboardEvent).nativeEvent as KeyboardEvent | undefined;
  const ev = native || (e as KeyboardEvent);
  if (typeof ev.isComposing === "boolean" && ev.isComposing) return false;
  return ev.keyCode !== 229;
}
