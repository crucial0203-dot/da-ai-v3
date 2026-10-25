"""V4.0 N5 联网搜索。

三档数据源：
- `bocha`   —— 博查 API，有 BOCHA_API_KEY 时优先（要 Key）；
- `zhipu`   —— 智谱 web-search API，要 ZHIPU_API_KEY；
- `bing_html` —— 无 Key 兜底：抓 cn.bing.com 的搜索结果页，解析标题/链接/摘要。
  明确标记为降级源——结果质量不如前两者，但零配置可用。

硬约束：
- 总量 ≤12000 字，每页 ≤4000 字；
- 抓正文禁内网地址（169.254/10./127./192.168./172.16-31/::1），
  否则就是一个 SSRF 出口；
- **不额外扣额度**：搜索是附赠能力，不额外消耗用户的每日次数。
"""
from __future__ import annotations

import asyncio
import base64
import html as html_lib
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

from .. import config

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# 自己跟进重定向时的跳数上限，防止无限重定向
_MAX_REDIRECTS = 3


@dataclass
class SearchHit:
    n: int = 0
    title: str = ""
    url: str = ""
    snippet: str = ""
    body: str = ""
    provider: str = ""
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "n": self.n, "title": self.title, "url": self.url,
            "snippet": self.snippet, "provider": self.provider,
            "chars": len(self.body),
        }


class WebSearchError(Exception):
    pass


# ---- query 清洗 ----
# 用户消息是对着助手说的口语整句，直接整句发给 Bing 会灾难性分词
# （实测「你能不能做PPT」→ 返回的全是单字「你」的百科/歌曲）。
# clean_query 只剥「客套外壳」，保留实义疑问词（「怎么做PPT」Bing 表现正常）。

# 会话外壳：只收客套/指代/请求模板，不收疑问实义词
_SHELL_PATTERNS = [
    r"^你可以?", r"^你能不能", r"^你会不会", r"^你会(不会)?", r"^能不能(帮我)?",
    r"^可不可以", r"^可以帮我", r"^麻烦你?", r"^请你?", r"^帮我", r"^帮个忙",
    r"^帮帮忙", r"^我想要?", r"^我想让你", r"^我想让你?", r"^我要你", r"^给我",
    r"^有没有", r"^有吗", r"^有没有人", r"^请问你?", r"^谢谢", r"^辛苦",
    r"(好不好|行不行|可以吗|好吗|嘛|吧|呢|吗)$",
    r"^(can|could)\s+you", r"^please", r"^help\s+me(\s+to)?", r"^i\s+want",
    r"^i'?d\s+like", r"^is\s+there\b", r"(for\s+me)\?*$",
]
_SHELL_RE = [re.compile(p, re.I) for p in _SHELL_PATTERNS]
# 剥完外壳后剩下的单字停用词（只剩这些 = 没有可搜索的实义）
_STOPWORDS = set("你我他她它您吗呢啊吧呀哦嘛啥诶的了吗哟恩哦喔哈喂")
_STOP_EN = {"a", "an", "the", "is", "are", "am", "do", "does", "did",
            "of", "to", "for", "on", "in", "at", "and", "or", "you", "me",
            "my", "i", "we", "it", "that", "this", "can", "could", "please"}


# 剥 markdown 记号、引号、句末标点。
# 用「字符集变量 + re.escape」拼正则：直接写含引号的 raw 串，
# 相邻片段一旦漏掉 r 前缀，\s 就会变成无效转义（SyntaxWarning）。
_TAIL_CHARS = "*_`#>~[]()「」『』\"'！!。.,，、;；:：?？"
_HEAD_CHARS = "*_`#>~[]()「」『』\"'，,、;；:："
_TAIL_MARKS = re.compile("[" + re.escape(_TAIL_CHARS) + r"\s]+$")
_HEAD_MARKS = re.compile("^[" + re.escape(_HEAD_CHARS) + r"\s]+")


def clean_query(text: str) -> str | None:
    """把用户消息清洗成搜索词。

    返回 None 表示「没有可搜索的实义」（纯客套/纯停用词），
    调用方应跳过联网搜索——诚实无来源好过乱来源。
    """
    import unicodedata

    s = unicodedata.normalize("NFKC", text or "").strip()
    if not s:
        return None
    s = _TAIL_MARKS.sub("", s)
    s = _HEAD_MARKS.sub("", s)
    # 迭代剥外壳（「麻烦你帮我查一下PPT」两层模板）
    for _ in range(4):
        before = s
        for pattern in _SHELL_RE:
            s = pattern.sub("", s).strip()
        if s == before:
            break
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None
    # 有效内容判定：中文有效字（非停用词单字）数 + 拉丁/数字词数（整串抽取，
    # 「做PPT」这类中英粘连 token 也能算出 PPT）
    en_words = re.findall(r"[A-Za-z0-9][A-Za-z0-9+\-._#]*", s)
    zh_chars = [c for c in s if "\u4e00" <= c <= "\u9fff"]
    zh_valid = [c for c in zh_chars if c not in _STOPWORDS]
    en_valid = [t for t in en_words if t.lower() not in _STOP_EN]
    if not zh_valid and not en_valid:
        return None
    # 只剩单个中文字且是停用词已在上面挡住；剩单字（如「帮」）也不值得搜
    if not en_valid and len(zh_valid) < 2:
        return None
    # 只剩叠词动词（看看/查查/说说…）没有宾语 = 无可搜索内容
    if not en_valid and len(zh_valid) == 2 and zh_valid[0] == zh_valid[1]:
        return None
    return s


# ---- 出口安全 ----

_PRIVATE_PREFIXES = ("127.", "10.", "192.168.", "169.254.", "0.", "localhost")


def is_private_host(host: str) -> bool:
    """判断主机是否指向内网/回环/保留段。DNS 解析后再判，防 DNS rebinding。"""
    if not host:
        return True
    lowered = host.lower()
    if lowered in {"localhost", "metadata.google.internal", "169.254.169.254"}:
        return True
    # 本地验收专用：SEARCH_ALLOW_LOOPBACK=1 时放行回环（线上与发布包都禁这个键）
    if config.SEARCH_ALLOW_LOOPBACK and (lowered in {"127.0.0.1", "::1"} or lowered.startswith("127.")):
        return False
    if any(lowered.startswith(p) for p in _PRIVATE_PREFIXES):
        return True
    try:
        addr = ipaddress.ip_address(lowered)
        return (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(lowered, None)
    except (socket.gaierror, UnicodeError, OSError):
        return True  # 解析不了 = 不放行
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast):
            return True
    return False


def safe_url(url: str) -> bool:
    """只允许 http/https 公网地址。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    return not is_private_host(parsed.hostname or "")


# ---- Provider 选择 ----

_PROVIDER_LABELS = {"bocha": "博查", "zhipu": "智谱", "bing_html": "Bing 网页"}


def provider_label(provider_id: str) -> str:
    """provider id → 给人看的中文名。前端来源列表不该直接显示 bing_html。"""
    return _PROVIDER_LABELS.get(provider_id, provider_id)


def available_providers() -> list[dict]:
    """返回各 provider 的可用状态（供 /__net 诊断与前端展示）。"""
    return [
        {
            "id": "bocha", "label": "博查",
            "available": bool(config.BOCHA_API_KEY),
            "degraded": False,
            "note": "有 BOCHA_API_KEY 时启用" if config.BOCHA_API_KEY else "未配置 Key",
        },
        {
            "id": "zhipu", "label": "智谱",
            "available": bool(config.ZHIPU_API_KEY),
            "degraded": False,
            "note": "有 ZHIPU_API_KEY 时启用" if config.ZHIPU_API_KEY else "未配置 Key",
        },
        {
            "id": "bing_html", "label": "Bing 网页",
            "available": True,
            "degraded": True,
            "note": "无 Key 兜底源，结果质量较低",
        },
    ]


def pick_provider() -> dict:
    """按配置与可用 Key 选一个源。settings.web_provider 可强制指定。"""
    wanted = (config.WEB_SEARCH_PROVIDER or "").strip()
    providers = available_providers()
    if wanted:
        for p in providers:
            if p["id"] == wanted:
                if not p["available"]:
                    raise WebSearchError(f"指定的搜索源 {wanted} 不可用：{p['note']}")
                return p
        raise WebSearchError(f"未知的搜索源：{wanted}")
    for p in providers:
        if p["available"] and not p["degraded"]:
            return p
    for p in providers:
        if p["available"]:
            return p
    raise WebSearchError("没有可用的搜索源")


# ---- 各 provider 的实现 ----

async def _http_get(url: str, headers: dict | None = None, timeout: float | None = None) -> str:
    """取正文，并对**每一跳**做内网地址校验。

    原来交给 httpx 的 `follow_redirects=True`：它一路跟到最后一个 Location，
    而我们只对跳转链的第一个 URL 做过 safe_url 检查——一个 302 指向
    http://169.254.169.254/... 就能把内网黑名单整个绕过去。这里改成自己跟进，
    最多 3 跳，每一跳重新校验。
    """
    import httpx

    timeout = timeout or config.WEB_SEARCH_TIMEOUT
    current = url
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=timeout,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
    ) as client:
        for _hop in range(_MAX_REDIRECTS + 1):
            if not safe_url(current):
                raise WebSearchError("目标地址不在可访问范围：内网地址一律不抓")
            resp = await client.get(current)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location") or ""
                if not location:
                    break
                current = urljoin(current, location)
                continue
            resp.raise_for_status()
            return resp.text
        raise WebSearchError(f"重定向次数超过上限（{_MAX_REDIRECTS} 跳）")


async def _search_bocha(query: str, top_k: int) -> list[SearchHit]:
    import httpx

    async with httpx.AsyncClient(timeout=config.WEB_SEARCH_TIMEOUT) as client:
        resp = await client.post(
            f"{config.BOCHA_BASE_URL}/v1/web-search",
            headers={"Authorization": f"Bearer {config.BOCHA_API_KEY}"},
            json={"query": query, "count": top_k, "summary": True},
        )
        resp.raise_for_status()
        data = resp.json()
    out: list[SearchHit] = []
    for item in ((data.get("data") or {}).get("webPages") or {}).get("value") or []:
        out.append(SearchHit(
            title=item.get("name") or "",
            url=item.get("url") or "",
            snippet=(item.get("summary") or item.get("snippet") or "")[:600],
            provider="bocha",
        ))
    return out


async def _search_zhipu(query: str, top_k: int) -> list[SearchHit]:
    import httpx

    async with httpx.AsyncClient(timeout=config.WEB_SEARCH_TIMEOUT) as client:
        resp = await client.post(
            f"{config.ZHIPU_BASE_URL}/web_search",
            headers={"Authorization": f"Bearer {config.ZHIPU_API_KEY}"},
            json={"search_engine": "search_std", "search_query": query},
        )
        resp.raise_for_status()
        data = resp.json()
    out: list[SearchHit] = []
    for item in (data.get("search_result") or [])[:top_k]:
        out.append(SearchHit(
            title=item.get("title") or "",
            url=item.get("link") or "",
            snippet=(item.get("content") or "")[:600],
            provider="zhipu",
        ))
    return out


_BING_LINK = re.compile(r'<h2[^>]*>\s*<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', re.S)
_BING_SNIPPET = re.compile(r'<p[^>]*>(.*?)</p>', re.S)
_TAG = re.compile(r"<[^>]+>")
# 摘要里的代码/脚手架痕迹（宁缺勿滥：脏摘要不如没有摘要）
_CODE_TRACE = re.compile(
    r"function\s*\(|=>|\{[" r"\w$]*:|;\s*var\s+|typeof\s+|undefined|window\.|\bvar\s+\w+\s*="
)
# 先剥脚本再找 <p>，否则 Bing 内联 JS 会被当摘要
_SCRIPT_BLOCK = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")

def _strip_tags(html: str) -> str:
    text = _TAG.sub(" ", html or "")
    # 用标准库解实体：Bing 摘要里会出现 &#242; 这类数字实体，
    # 手写 replace 只覆盖了十来个命名实体，剩下的会原样显示成乱码。
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _unwrap_bing(url: str) -> str:
    """还原 Bing 的跳转链。

    Bing 搜索结果常把真实地址包成
    `https://www.bing.com/ck/a?...&u=a1<base64url>`；直接把外层链接存下来，
    前端来源列表和正文抓取都会拿到 Bing 的中间页，而不是原文。
    `u` 参数以 a1 开头，后面是目标 URL 的 base64url 编码。
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    host = (parsed.hostname or "").lower()
    if not host.endswith("bing.com") or parsed.path != "/ck/a":
        return url
    value = (parse_qs(parsed.query).get("u") or [""])[0]
    if not value.startswith("a1"):
        return url
    payload = value[2:]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        target = raw.decode("utf-8", "ignore").strip()
    except (ValueError, TypeError):
        return url
    return target if target.startswith(("http://", "https://")) else url


async def _search_bing(query: str, top_k: int) -> list[SearchHit]:
    url = f"{config.BING_BASE_URL}/search?q={quote_plus(query)}&ensearch=0"
    html = await _http_get(url)
    hits: list[SearchHit] = []
    for match in _BING_LINK.finditer(html):
        link = _unwrap_bing(match.group(1))
        title = _strip_tags(match.group(2))
        if not title or not safe_url(link):
            continue
        hits.append(SearchHit(title=title, url=link, snippet="", provider="bing_html"))
        if len(hits) >= top_k:
            break
    # 摘要：取结果块里的 <p>，顺序大致对应
    snippets = [_strip_tags(m.group(1)) for m in _BING_SNIPPET.finditer(_SCRIPT_BLOCK.sub(" ", html))]
    for index, hit in enumerate(hits):
        if index < len(snippets):
            snippet = snippets[index][:600]
            # 抓到的是脚本碎片/代码痕迹 → 弃用
            if snippet and _CODE_TRACE.search(snippet):
                snippet = ""
            hit.snippet = snippet
    return hits


async def _search(provider_id: str, query: str, top_k: int) -> list[SearchHit]:
    if provider_id == "bocha":
        return await _search_bocha(query, top_k)
    if provider_id == "zhipu":
        return await _search_zhipu(query, top_k)
    return await _search_bing(query, top_k)


# ---- 正文抓取 ----

async def fetch_body(url: str) -> str:
    """抓正文并抽文本。内网地址直接拒绝。"""
    if not safe_url(url):
        raise WebSearchError("目标地址不是公网 http/https，已拒绝")
    html = await _http_get(url)
    # 去掉 script/style，再抽文本
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = _strip_tags(html)
    return text[:config.WEB_SEARCH_MAX_PAGE_CHARS]


async def _enrich(hit: SearchHit) -> None:
    try:
        hit.body = await fetch_body(hit.url)
    except Exception:
        hit.body = ""


def _dedupe(hits: list[SearchHit]) -> list[SearchHit]:
    """按 URL 去重（去掉片段与末尾斜杠）。

    Bing/Bocha 的首页结果常有同一篇文章的不同入口（移动版、跳转链还原后
    重复），不去重会让来源列表出现两三条一模一样的链接。
    """
    seen: set[str] = set()
    out: list[SearchHit] = []
    for hit in hits:
        key = (hit.url or "").strip().split("#", 1)[0].rstrip("/")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


# ---- 对外主入口 ----

async def search(query: str, top_k: int | None = None) -> tuple[list[SearchHit], str]:
    """执行一次检索。

    返回 (hits, provider_id)。hits 里的 n 是引用编号（1 起）。
    正文抓取在后台并发做，但总量控制在 12000 字以内。
    """
    query = (query or "").strip()
    if not query:
        return [], ""
    top_k = max(1, min(int(top_k or config.WEB_SEARCH_TOP_K), 10))
    provider = pick_provider()

    hits = await _search(provider["id"], query, top_k)
    hits = _dedupe(hits)
    for index, hit in enumerate(hits):
        hit.n = index + 1

    # 并发抓正文，但总量封顶。
    #
    # 先一起抓、再按排名顺序分配预算：原实现是各协程边抓边扣同一个
    # budget，谁先返回谁先占满——排名第一的结果反而可能一个字都分不到，
    # 而且结果随网络时序变来变去。现在分配是确定性的：预算优先给排名高的。
    async def body_of(url: str) -> str:
        try:
            return await fetch_body(url)
        except Exception:
            return ""

    bodies = await asyncio.gather(*(body_of(h.url) for h in hits))
    budget = config.WEB_SEARCH_MAX_TOTAL_CHARS
    for hit, body in zip(hits, bodies):
        if not body or budget <= 0:
            hit.body = ""
            continue
        take = min(len(body), budget, config.WEB_SEARCH_MAX_PAGE_CHARS)
        hit.body = body[:take]
        budget -= take
    return hits, provider["id"]


def format_results(hits: list[SearchHit]) -> str:
    """把检索结果格式化成注入模型的独立块，带 [n] 引用编号。"""
    if not hits:
        return ""
    parts = ["<<<WEB_RESULTS>>>"]
    for hit in hits:
        title = hit.title.strip() or hit.url
        parts.append(f"[{hit.n}] {title}\n来源：{hit.url}\n{hit.snippet}".strip())
        if hit.body:
            parts.append(f"正文摘录：{hit.body}")
    parts.append("<<<END_WEB_RESULTS>>>")
    return "\n\n".join(parts)


def sources_payload(hits: list[SearchHit]) -> list[dict]:
    """落库用的精简来源列表。

    provider 同时给 id 与中文名：老前端读 provider，新前端优先读
    provider_label，不会把 bing_html 这种内部 id 直接显示给用户。
    """
    return [
        {
            "n": h.n, "title": h.title, "url": h.url,
            "provider": h.provider, "provider_label": provider_label(h.provider),
        }
        for h in hits
    ]
