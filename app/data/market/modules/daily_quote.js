/**
 * 每日一言 —— 注册到 sidebar.footer 槽位。
 * 演示：动态加载的 UI 模块拿到宿主 ctx（React/槽位注册），返回 disposer 可逆。
 */
const QUOTES = [
  "法律的尽头是经验，经验的尽头是判决。——霍姆斯",
  "正义不仅应得到实现，而且要以人们看得见的方式实现。——贝卡利亚",
  "读书譬如饮食，从容咀嚼，其味必长。——朱熹",
  "风物长宜放眼量。——毛泽东",
  "博学之，审问之，慎思之，明辨之，笃行之。——《礼记》",
  "为学之道，必本于思。——晁说之",
  "操千曲而后晓声，观千剑而后识器。——刘勰",
  "法者，治之端也。——荀子",
];

export default {
  manifest: { id: "ui.daily-quote", name: "每日一言", version: "1.1.0" },
  activate(ctx) {
    const { React } = ctx;
    const h = React.createElement;

    function QuoteCard() {
      const day = Math.floor(Date.now() / 86400000);
      const [idx, setIdx] = React.useState(day % QUOTES.length);
      return h(
        "div",
        {
          "data-testid": "plugin-daily-quote",
          style: {
            margin: "10px 10px 4px",
            padding: "10px 12px",
            border: "1px solid var(--line)",
            borderRadius: "var(--radius)",
            background: "var(--surface-2)",
            cursor: "pointer",
            fontSize: "var(--fs-13)",
            lineHeight: 1.6,
            color: "var(--text-2)",
          },
          title: "点击换一句",
          onClick: () => setIdx((i) => (i + 1) % QUOTES.length),
        },
        h("div", { style: { color: "var(--muted)", fontSize: "var(--fs-12)", marginBottom: 4 } }, "每日一言"),
        h("div", null, QUOTES[idx])
      );
    }

    ctx.slots.register("sidebar.footer", { id: "quote-card", component: QuoteCard, order: 10 });
  },
};
