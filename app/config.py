"""统一配置入口。

规则：代码里不出现硬编码的数据路径。生产用 /opt/da-data（实测跨发布保留），
本地用 app/var，由 DATA_DIR 环境变量决定，切换环境不改代码。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _flag(key: str, default: bool = False) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


APP_NAME = "DA的AI"
# DEBUG 必须最先定义：下面多处 `xxx and DEBUG` 引用它，
# 前向引用会在 Python 按行求值时抛 NameError（历史上 WEB_SEARCH_FAKE=1 直接让进程起不来）。
DEBUG = _flag("DEBUG_MODE", False)
MODEL = _env("MINIMAX_MODEL", "MiniMax-M3.1-Flash")
MINIMAX_BASE_URL = _env("MINIMAX_BASE_URL", "https://api.minimax.cn/anthropic").rstrip("/")
MINIMAX_API_KEY = _env("MINIMAX_API_KEY")
DEFAULT_EFFORT = _env("DEFAULT_EFFORT", "medium")

SECRET_KEY = _env("SECRET_KEY")
ADMIN_TOKEN = _env("DEBUG_TOKEN") or _env("ADMIN_TOKEN")
ADMIN_NICKNAME = _env("ADMIN_NICKNAME", "泉达")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "")

# 数据目录：Linux 服务器默认 /opt/da-staff-data（实测跨发布保留），
# Windows 本地开发默认 app/var；两者都可用 DATA_DIR 环境变量覆盖。
_default_data = Path("/opt/da-data") if os.name != "nt" else (BASE_DIR / "var")
DATA_DIR = Path(_env("DATA_DIR") or _default_data).expanduser().resolve()
FILES_DIR = DATA_DIR / "files"
DB_PATH = DATA_DIR / "app.db"

STATIC_DIR = BASE_DIR / "static"

# V4.0 N2 起：新前端（React 构建产物）是唯一前端。开关保留给应急回滚
# （回滚 = 重发上一版 dist，而不是回旧静态页——旧静态页已删除）。
NEW_FRONTEND = _flag("NEW_FRONTEND", True)

# ---- V4.0 N4 轻量 RAG ----
RAG_ENABLED = _flag("RAG_ENABLED", True)
RAG_CHUNK_CHARS = int(_env("RAG_CHUNK_CHARS", "600"))
RAG_CHUNK_OVERLAP = int(_env("RAG_CHUNK_OVERLAP", "80"))
RAG_TOP_K = int(_env("RAG_TOP_K", "6"))
RAG_MAX_CHUNK_CHARS = int(_env("RAG_MAX_CHUNK_CHARS", "800"))
RAG_MAX_DOC_CHARS = int(_env("RAG_MAX_DOC_CHARS", "2000000"))

# ---- V4.0 N5 联网搜索 ----
WEB_SEARCH_ENABLED = _flag("WEB_SEARCH_ENABLED", True)
WEB_SEARCH_TOP_K = int(_env("WEB_SEARCH_TOP_K", "5"))
WEB_SEARCH_MAX_TOTAL_CHARS = int(_env("WEB_SEARCH_MAX_TOTAL_CHARS", "12000"))
WEB_SEARCH_MAX_PAGE_CHARS = int(_env("WEB_SEARCH_MAX_PAGE_CHARS", "4000"))
WEB_SEARCH_TIMEOUT = float(_env("WEB_SEARCH_TIMEOUT", "8"))
WEB_SEARCH_FAKE = _flag("WEB_SEARCH_FAKE", False) and DEBUG
# 口语整句直发 Bing 会灾难性分词（「你能不能做PPT」→ 全是单字「你」的结果）。
# clean_query 开关：出问题可一键关回旧行为。
WEB_SEARCH_CLEAN_QUERY = _flag("WEB_SEARCH_CLEAN_QUERY", True)
SEARCH_ALLOW_LOOPBACK = _flag("SEARCH_ALLOW_LOOPBACK", False)
BOCHA_API_KEY = _env("BOCHA_API_KEY")
ZHIPU_API_KEY = _env("ZHIPU_API_KEY")
WEB_SEARCH_PROVIDER = _env("WEB_SEARCH_PROVIDER", "")
# base_url 可覆盖：本地验收时指向罐头服务器（SEARCH_ALLOW_LOOPBACK=1 门禁）
BOCHA_BASE_URL = _env("BOCHA_BASE_URL", "https://api.bochaai.com").rstrip("/")
ZHIPU_BASE_URL = _env("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
BING_BASE_URL = _env("BING_BASE_URL", "https://cn.bing.com").rstrip("/")

# ---- V4.0 N6 工具回路 ----
TOOLS_ENABLED = _flag("TOOLS_ENABLED", True)
MAX_TOOL_ROUNDS = int(_env("MAX_TOOL_ROUNDS", "3"))
TOOL_TIMEOUT = float(_env("TOOL_TIMEOUT", "30"))

# ---- V4.0 N7 记忆 ----
MEMORY_ENABLED = _flag("MEMORY_ENABLED", True)
MEMORY_MAX_ITEMS = int(_env("MEMORY_MAX_ITEMS", "20"))
MEMORY_MAX_ITEM_CHARS = int(_env("MEMORY_MAX_ITEM_CHARS", "300"))

# ---- V4.0 N8 制品 ----
ARTIFACTS_ENABLED = _flag("ARTIFACTS_ENABLED", True)
ARTIFACT_MAX_CHARS = int(_env("ARTIFACT_MAX_CHARS", "60000"))

# ---- V4.0 N9 工作流 ----
WORKFLOW_ENABLED = _flag("WORKFLOW_ENABLED", True)
WORKFLOW_MAX_NODES = int(_env("WORKFLOW_MAX_NODES", "60"))
WORKFLOW_MAX_EDGES = int(_env("WORKFLOW_MAX_EDGES", "200"))

# 上传与配额
MAX_UPLOAD_BYTES = int(_env("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
# 图片单独设限：它会整段转 base64 进上下文，比文档更耗 token
MAX_IMAGE_UPLOAD_BYTES = int(_env("MAX_IMAGE_UPLOAD_BYTES", str(8 * 1024 * 1024)))
MAX_BATCH_FILES = int(_env("MAX_BATCH_FILES", "5"))
MAX_ZIP_ENTRIES = 200
MAX_ZIP_UNCOMPRESSED = 100 * 1024 * 1024
# 额度以「次」计，主额度由邀请码决定（invites.daily_quota，V3.1 起）。
# 这里只是兜底：码没设额度/迁移降级时才落到这个值。
DAILY_CALL_QUOTA = int(_env("DAILY_CALL_QUOTA", "5"))
# token 只做熔断，不做主配额，防止单次超长请求把成本顶穿
DAILY_TOKEN_CAP = int(_env("DAILY_TOKEN_CAP", "200000"))
RESERVE_TTL_SEC = int(_env("RESERVE_TTL_SEC", "600"))
# 日志模式：留空则启动时自检（WAL 并发写不通就退回 DELETE）；也可显式指定 wal/delete
DB_JOURNAL = _env("DB_JOURNAL", "")

SESSION_COOKIE = "da_sid"
SESSION_TTL_DAYS = int(_env("SESSION_TTL_DAYS", "30"))
ADMIN_SESSION_HOURS = int(_env("ADMIN_SESSION_HOURS", "12"))
SECURE_COOKIE = _flag("SECURE_COOKIE", True)

# 由反向代理托管时，平台可能把 Host 改写成内部地址，浏览器发的 Origin 仍是公网域名。
# 在这里声明本站的公开来源（如 https://da-ai.app.workbuddy.host），同源校验才不会误杀。
PUBLIC_ORIGIN = _env("PUBLIC_ORIGIN", "").rstrip("/")

ALLOWED_UPLOAD_EXT = {
    ".pdf", ".docx", ".pptx", ".md", ".txt", ".csv",
    ".png", ".jpg", ".jpeg", ".webp",
}

# 能走多模态视觉的扩展名。只有 vision=True 的模型才会真的读到它们，
# 纯文本模型拿到图片不报错也不读（实测会拿文字硬猜），所以必须提前拦。
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}

# 视觉链路限制：一道总闸 + 四个上限。出问题把 VISION_ENABLED 关掉即可回到纯文本链路。
VISION_ENABLED = _flag("VISION_ENABLED", True)
MAX_IMAGES_PER_TURN = int(_env("MAX_IMAGES_PER_TURN", "4"))
MAX_IMAGES_PER_TASK = int(_env("MAX_IMAGES_PER_TASK", "4"))
VISION_MAX_EDGE = int(_env("VISION_MAX_EDGE", "1568"))
VISION_JPEG_QUALITY = int(_env("VISION_JPEG_QUALITY", "85"))
# 原图小于这个体积就直传，不重编码；否则缩放后转 JPEG 控 token
VISION_SEND_RAW_BYTES = int(_env("VISION_SEND_RAW_BYTES", str(1200 * 1024)))
# base64 累计上限，兜住极端尺寸（单张 8MB 的图 base64 后约 11MB）
MAX_VISION_B64_TOTAL = int(_env("MAX_VISION_B64_TOTAL", str(12 * 1024 * 1024)))

# 文件保留与配额（第一期不做对象存储，靠这两条兜住磁盘）
FILE_RETAIN_DAYS = int(_env("FILE_RETAIN_DAYS", "30"))
MAX_VISITOR_BYTES = int(_env("MAX_VISITOR_BYTES", str(200 * 1024 * 1024)))

MAX_TOKENS_CHAPTER = int(_env("MAX_TOKENS_CHAPTER", "8000"))
MAX_TOKENS_CHAT = int(_env("MAX_TOKENS_CHAT", "8192"))
UPSTREAM_TIMEOUT = float(_env("UPSTREAM_TIMEOUT", "300"))
# DEBUG 已在文件顶部定义（见那里的注释），此处不再重复声明
FAKE_UPSTREAM = _flag("FAKE_UPSTREAM", False) and DEBUG


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)


def data_dir_hint() -> str:
    """诊断用：说明数据实际落在哪里。"""
    return str(DATA_DIR)
