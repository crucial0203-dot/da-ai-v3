"""V4.0 N8 制品（Artifacts）。

模型可以输出一段 HTML/CSS/JS 来做一个可预览的小制品（图表、表格、页面草图）。
抽取靠围栏代码块：

```html
...完整 HTML...
```

安全要点（这几条是硬约束，不是风格偏好）：
1. **不用 srcdoc**——srcdoc 会继承父页面 origin，等于把用户页面同源暴露给模型输出；
   制品走独立路由 `/api/artifacts/{id}/render`，它有自己的 CSP。
2. **iframe sandbox 不给 allow-same-origin**——渲染页面处于独立（不透明）origin，
   访问 parent.document 会抛 SecurityError。
3. **render 路由的 CSP 极严**：`default-src 'none'`，只放开 inline script/style
   与 data:/blob: 图片；`connect-src 'none'`（不能发请求）、`frame-ancestors 'self'`。
"""
from __future__ import annotations

import html as html_lib
import re

from .. import config, db

# ```html ... ``` / ```css ... ``` / ```js ... ``` 围栏
_HTML_BLOCK = re.compile(r"```html\s*\n?([\s\S]*?)\n?```", re.IGNORECASE)
_CSS_BLOCK = re.compile(r"```css\s*\n?([\s\S]*?)\n?```", re.IGNORECASE)
_JS_BLOCK = re.compile(r"```js\s*\n?([\s\S]*?)\n?```", re.IGNORECASE)
_HTML_TAG = re.compile(r"```html\s*\n?([\s\S]*?)\n?```", re.IGNORECASE)

# 字数统计用：先剔掉 script/style 整块，再去标签、反转义实体、去空白。
_BLOCK = re.compile(r"<(script|style)\b[^>]*>[\s\S]*?</\1>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")

# 组装完整文档时固定的模板开销（DOCTYPE/head/meta/body 等），
# 版本写入的尺寸校验放宽这么多，避免模板本身把合法内容顶出上限。
DOC_OVERHEAD = 4096

# 不允许出现在制品里的危险协议
_DANGEROUS = (
    "javascript:", "data:text/html", "vbscript:",
    "<script src=", "<iframe", "<object", "<embed", "<link rel=\"stylesheet\" href=\"http",
)


def _sanitize(html: str) -> str:
    """去掉明显越界的东西。

    这是**第二道**防线——真正的隔离靠独立 CSP + iframe sandbox。
    这里只是把最蠢的越界方式先挡掉，避免用户自己看到坏东西。

    注意：inline script **保留**——制品的交互就靠它（用 onclick 之类）。
    只清掉引用外部资源的 script / link / iframe / object / embed。
    """
    out = html or ""
    # 外部资源引用（只留 inline）
    out = re.sub(r"<script[^>]+src=[^>]*>.*?</script>", "", out, flags=re.I | re.S)
    out = re.sub(r"<link[^>]+href=[\"']https?://[^\"']*[\"'][^>]*>", "", out, flags=re.I)
    out = re.sub(r"<iframe[^>]*>.*?</iframe>", "", out, flags=re.I | re.S)
    out = re.sub(r"<object[^>]*>.*?</object>", "", out, flags=re.I | re.S)
    out = re.sub(r"<embed[^>]*>", "", out, flags=re.I)
    # 危险协议：只替换协议头，不动整段文本
    out = re.sub(r"javascript:", "blocked:", out, flags=re.I)
    out = re.sub(r"vbscript:", "blocked:", out, flags=re.I)
    out = re.sub(r"data:text/html", "blocked:data", out, flags=re.I)
    return out


def count_chars(document: str) -> int:
    """制品字数：可见正文字符数。

    卡片上的「N 字」是给用户判断内容量用的，HTML 标签、CSS、JS 都不算字。
    （以前用 LENGTH(html)，一份带样式的文档会被算成几万字。）
    """
    text = _BLOCK.sub("", document or "")
    text = _TAG.sub("", text)
    text = html_lib.unescape(text)
    return len(_SPACE.sub("", text))


def extract_blocks(text: str) -> dict[str, str]:
    """从模型正文里抽出 html/css/js 三块。"""
    html = ""
    match = _HTML_BLOCK.search(text or "")
    if match:
        html = match.group(1)
    css = ""
    match = _CSS_BLOCK.search(text or "")
    if match:
        css = match.group(1)
    js = ""
    match = _JS_BLOCK.search(text or "")
    if match:
        js = match.group(1)
    return {"html": html, "css": css, "js": js}


def build_document(html: str, css: str = "", js: str = "") -> str:
    """把三块拼成一份完整 HTML 文档。

    拼装顺序：html 正文 → <style> → <script>。CSS/JS 块里的内容原样注入
    （由 CSP 的 'unsafe-inline' 放行）。

    html 正文先过一遍 _sanitize：越界内容在入库前就清掉，
    不靠渲染时的 CSP 兜底（那是第二道防线）。
    """
    html = _sanitize(html or "")
    if "<html" in html.lower():
        # 已经是完整文档：把 css/js 插进 head/body 之前的 </body>
        doc = html
        inject = ""
        if css:
            inject += f"<style>\n{css}\n</style>\n"
        if js:
            inject += f"<script>\n{js}\n</script>\n"
        if inject:
            if "</body>" in doc.lower():
                doc = re.sub(r"</body>", inject + "</body>", doc, flags=re.I)
            else:
                doc += inject
        return doc
    parts = [
        "<!DOCTYPE html>",
        "<html lang=\"zh-CN\">",
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
    ]
    if css:
        parts.append(f"<style>\n{css}\n</style>")
    parts.append("</head>")
    parts.append("<body>")
    parts.append(html)
    if js:
        parts.append(f"<script>\n{js}\n</script>")
    parts.append("</body></html>")
    return "\n".join(parts)


def create_artifact(user_id: str, title: str, html: str, css: str = "", js: str = "",
                    task_id: str | None = None, message_id: int | None = None) -> str:
    """创建一个制品，返回 id。"""
    if not config.ARTIFACTS_ENABLED:
        raise ValueError("制品功能未开启")
    # 三块都要算进上限：只查 html 的话，css/js 能绕过尺寸限制。
    if len(html or "") + len(css or "") + len(js or "") > config.ARTIFACT_MAX_CHARS:
        raise ValueError(f"制品内容超过 {config.ARTIFACT_MAX_CHARS} 字上限")
    document = build_document(html, css, js)
    art_id = db.new_id("a_")
    db.execute(
        "INSERT INTO artifacts(id, user_id, task_id, message_id, title, language, html, version, created_at, chars) "
        "VALUES(?,?,?,?,?,'html',?,1,?,?)",
        (art_id, user_id, task_id, message_id, (title or "未命名制品")[:80],
         document, db.now_iso(), count_chars(document)),
    )
    return art_id


def get_artifact(artifact_id: str, user_id: str) -> dict | None:
    row = db.one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
    if not row:
        return None
    if row["user_id"] != user_id:
        return None
    return dict(row)


def list_artifacts(user_id: str, task_id: str | None = None) -> list[dict]:
    # P1-7：带出正文字数（chars）。列表接口不取正文本身——可能几十 KB。
    sql = (
        "SELECT id, title, language, version, task_id, message_id, created_at, chars "
        "FROM artifacts WHERE user_id = ?"
    )
    args: list = [user_id]
    if task_id:
        sql += " AND task_id = ?"
        args.append(task_id)
        # created_at 只到秒：同一秒建的多个制品靠 rowid 定先后，
        # 否则「最新一张」可能取到先写的那张
        sql += " ORDER BY created_at DESC, rowid DESC"
    else:
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT 200"
    rows = db.rows(sql, tuple(args))
    return [dict(r) for r in rows]


def _root_id(artifact_id: str) -> str:
    """沿 parent_id 往上找根制品 id。

    版本链是 `parent_id` 串起来的树：v2 的父是 v1，v3 的父可能是 v1。
    版本号要按**同一条链**递增，所以先归到根上再算。
    """
    current = artifact_id
    for _ in range(20):  # 防环：正常链不超过 20 层
        row = db.one("SELECT id, parent_id FROM artifacts WHERE id = ?", (current,))
        if not row or not row["parent_id"]:
            return current
        current = row["parent_id"]
    return current


def _max_version_in_tree(root: str) -> int:
    """同一条版本链里的最大版本号。

    链是 parent_id 串起来的树，用递归 CTE 把整棵子树捞出来。
    只看直接子节点是不够的：恢复 v1 会生成挂在 v1 下的新版本，
    直接子节点里的最大版本号会漏掉另一支上的版本。
    """
    row = db.one(
        "WITH RECURSIVE chain(id, version) AS ("
        "  SELECT id, version FROM artifacts WHERE id = ?"
        "  UNION ALL"
        "  SELECT a.id, a.version FROM artifacts a JOIN chain c ON a.parent_id = c.id"
        ") SELECT MAX(version) AS v FROM chain",
        (root,),
    )
    return int((row["v"] if row and row["v"] else 1) or 1)


def create_version(artifact_id: str, user_id: str, html: str) -> str:
    """基于已有制品创建新版本。"""
    original = get_artifact(artifact_id, user_id)
    if not original:
        raise ValueError("制品不存在")
    if len(html or "") > config.ARTIFACT_MAX_CHARS + DOC_OVERHEAD:
        raise ValueError(f"制品内容超过 {config.ARTIFACT_MAX_CHARS} 字上限")
    new_id = db.new_id("a_")
    root = _root_id(artifact_id)
    version = _max_version_in_tree(root) + 1
    db.execute(
        "INSERT INTO artifacts(id, user_id, task_id, message_id, title, language, html, version, parent_id, created_at, chars) "
        "VALUES(?,?,?,?,?,'html',?,?,?,?,?)",
        (new_id, original["user_id"], original["task_id"], original["message_id"],
         original["title"], html, version, artifact_id, db.now_iso(), count_chars(html)),
    )
    return new_id


def restore_version(artifact_id: str, user_id: str) -> str:
    """把某个历史版本恢复成新版本（不覆盖历史，保留审计）。"""
    original = get_artifact(artifact_id, user_id)
    if not original:
        raise ValueError("制品不存在")
    return create_version(artifact_id, user_id, original["html"])


# 渲染路由专用 CSP：极严，只放开 inline 与 data/blob 图片
RENDER_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline' data:; "
    "img-src data: blob:; "
    "font-src data:; "
    "connect-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'self'"
)
