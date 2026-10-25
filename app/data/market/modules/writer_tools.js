/**
 * 写作快捷条 —— 注册到 home.afterComposer 槽位。
 * 点击指令把提示词填进首页输入框（[data-testid=composer-input]）。
 */
const CMDS = [
  { label: "列大纲", text: "请为以下主题列一份分层大纲，每层给出要点：" },
  { label: "润色", text: "请润色下面这段文字，保持原意，提升表达：" },
  { label: "缩写", text: "请把下面这段文字压缩到一半篇幅，保留关键信息：" },
  { label: "改正式", text: "请把下面这段话改成正式书面语体：" },
  { label: "起标题", text: "请为下面的内容起 5 个标题备选：" },
];

export default {
  manifest: { id: "ui.writer-tools", name: "写作快捷条", version: "1.0.2" },
  activate(ctx) {
    const { React } = ctx;
    const h = React.createElement;

    function fill(text) {
      const el = document.querySelector("[data-testid=composer-input]");
      if (!el) {
        ctx.toast("没有找到输入框，请先回到首页", "error");
        return;
      }
      const proto = Object.getPrototypeOf(el);
      const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
      if (setter) setter.call(el, text);
      else el.value = text;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.focus();
    }

    function WriterBar() {
      return h(
        "div",
        {
          style: { display: "flex", flexWrap: "wrap", gap: 6, justifyContent: "center", marginTop: 10 },
        },
        CMDS.map((c) =>
          h(
            "button",
            {
              key: c.label,
              className: "chip",
              style: { cursor: "pointer" },
              onClick: () => fill(c.text),
            },
            c.label
          )
        )
      );
    }

    ctx.slots.register("home.afterComposer", { id: "writer-bar", component: WriterBar, order: 20 });
  },
};
