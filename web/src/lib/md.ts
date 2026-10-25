/**
 * 自研 Markdown 渲染：先转义再解析，只放行白名单标签，杜绝模型输出注入。
 * 从旧前端 md.js 移植，规则不变。
 */

function escapeHtml(text: unknown): string {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function inline(text: string): string {
  let out = escapeHtml(text);
  const codes: string[] = [];
  out = out.replace(/`([^`]+)`/g, (_, code: string) => {
    codes.push(code);
    return `\u0000${codes.length - 1}\u0000`;
  });
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  out = out.replace(/\u0000(\d+)\u0000/g, (_, i: string) => `<code>${codes[Number(i)]}</code>`);
  return out;
}

function renderTable(lines: string[]): string {
  const rows = lines.map((line) =>
    line
      .trim()
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map((c) => c.trim()),
  );
  if (rows.length < 2) return "";
  const head = rows[0];
  const body = rows.slice(2);
  const headHtml = head.map((c) => `<th>${inline(c)}</th>`).join("");
  const bodyHtml = body.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("");
  return `<table><thead><tr>${headHtml}</tr></thead><tbody>${bodyHtml}</tbody></table>`;
}

export function renderMarkdown(src: unknown): string {
  const lines = String(src ?? "")
    .replace(/\r\n/g, "\n")
    .split("\n");
  const html: string[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (/^```/.test(line.trim())) {
      const lang = line.trim().replace(/^```/, "").trim();
      const buf: string[] = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i].trim())) {
        buf.push(lines[i]);
        i += 1;
      }
      i += 1;
      const codeBody = buf.join("\n");
      if (lang.toLowerCase() === "tool") {
        // 工具调用块：折叠卡片展示（正文里出现它是调用记录，不是给读者看的裸 JSON）
        let toolName = "";
        try {
          const parsed = JSON.parse(codeBody) as Record<string, unknown>;
          toolName = String(parsed.name || parsed.tool || "");
        } catch {
          /* 解析不了就只显示原始内容 */
        }
        html.push(
          `<details class="tool-fence"><summary>⚙ 工具调用${toolName ? `：${escapeHtml(toolName)}` : ""}</summary>` +
            `<pre class="tool-fence-body"><code>${escapeHtml(codeBody)}</code></pre></details>`,
        );
        continue;
      }
      html.push(`<pre><code${lang ? ` class="lang-${escapeHtml(lang)}"` : ""}>${escapeHtml(codeBody)}</code></pre>`);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = Math.min(heading[1].length, 4);
      html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      i += 1;
      continue;
    }

    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
      html.push("<hr>");
      i += 1;
      continue;
    }

    if (/^\s*>/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      html.push(`<blockquote>${inline(buf.join(" "))}</blockquote>`);
      continue;
    }

    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        buf.push(lines[i]);
        i += 1;
      }
      html.push(renderTable(buf));
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*[-*+]\s+/, ""));
        i += 1;
      }
      html.push(`<ul>${buf.map((t) => `<li>${inline(t)}</li>`).join("")}</ul>`);
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*\d+[.)]\s+/, ""));
        i += 1;
      }
      html.push(`<ol>${buf.map((t) => `<li>${inline(t)}</li>`).join("")}</ol>`);
      continue;
    }

    if (!line.trim()) {
      i += 1;
      continue;
    }

    const buf: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\s*>|\s*[-*+]\s|\s*\d+[.)]\s)/.test(lines[i])) {
      buf.push(lines[i]);
      i += 1;
    }
    html.push(`<p>${inline(buf.join("<br>"))}</p>`);
  }
  return html.join("");
}

export function plainPreview(src: unknown, max = 120): string {
  const text = String(src ?? "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/[#>*`_|-]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return text.length > max ? `${text.slice(0, max)}…` : text;
}
