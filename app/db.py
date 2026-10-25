"""SQLite 访问层：建表、连接、事务、种子数据。

多线程（uvicorn 默认线程池）下每线程独立连接，不做全局连接复用；
WAL 模式让读不阻塞写，单机小并发足够。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, TypeVar

from . import config

TZ = timezone(timedelta(hours=8))  # 用户在广州，日配额按东八区切日
T = TypeVar("T")


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:16]
    return f"{prefix}{raw}" if prefix else raw


# ---- 连接与事务 ----
#
# 日志模式只设一次（它是库文件自身的持久属性，每次连接都设纯属浪费）。
# WAL 在 Linux 上可靠，但本机 Windows 实测有坑：并发新开连接时只有第一个
# 能拿到写权，其余全部报 "attempt to write a readonly database"（D 盘 11/12、
# C 盘 TEMP 2/12，换 DELETE / TRUNCATE 则 0/12）。所以启动时先自检，
# WAL 不可用就退回 DELETE——单进程小并发下 DELETE 完全够用。

TRACE = ("readonly database", "database is locked", "database table is locked")
_journal: str | None = None
_journal_lock = threading.Lock()


def is_transient(exc: BaseException) -> bool:
    """瞬时故障：换条连接重来就能过，不是逻辑错误。"""
    return isinstance(exc, sqlite3.OperationalError) and any(
        key in str(exc).lower() for key in TRACE
    )


def _probe_wal(folder) -> bool:
    """按真实并发形态试一次：N 线程各自新开连接同时写。

    只要有一个报只读，就判定本机 WAL 不可用。
    """
    probe = folder / "wal_probe.db"
    workers = 4
    result = {"ok": True}

    def attempt() -> None:
        conn = sqlite3.connect(str(probe), timeout=10.0)
        try:
            conn.execute("INSERT INTO probe(v) VALUES(1)")
            conn.commit()
        finally:
            conn.close()

    try:
        for suffix in ("", "-wal", "-shm"):
            p = probe.parent / (probe.name + suffix)
            if p.exists():
                p.unlink()
        init = sqlite3.connect(str(probe), timeout=10.0)
        try:
            init.execute("PRAGMA journal_mode=WAL")
            init.execute("CREATE TABLE probe(v INTEGER)")
            init.commit()
        finally:
            init.close()

        errors: list[BaseException] = []
        threads = [
            threading.Thread(
                target=lambda: _safe(attempt, errors), daemon=True
            )
            for _ in range(workers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        result["ok"] = not errors
    except sqlite3.Error:
        result["ok"] = False
    finally:
        for suffix in ("", "-wal", "-shm"):
            p = probe.parent / (probe.name + suffix)
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
    return bool(result["ok"])


def _safe(fn: Callable[[], None], sink: list[BaseException]) -> None:
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001
        sink.append(exc)


def journal_mode() -> str:
    """返回实际使用的日志模式。首次调用时定下来，之后不再变。"""
    global _journal
    if _journal:
        return _journal
    with _journal_lock:
        if _journal:
            return _journal
        want = config.DB_JOURNAL.lower()
        if want in {"wal", "delete", "truncate", "persist"}:
            _journal = want
        else:
            config.ensure_dirs()
            _journal = "wal" if _probe_wal(config.DATA_DIR) else "delete"
        return _journal


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(str(config.DB_PATH), timeout=20.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def atomic(fn: Callable[[sqlite3.Connection], T], retries: int = 6) -> T:
    """在写事务里执行 fn(conn)，遇瞬时只读/锁冲突换新连接整块重跑。

    重跑是安全的：块内只做数据库写，失败即整块回滚，不会留下半截状态。
    """
    delay = 0.02
    for attempt in range(retries):
        conn = connect()
        conn.isolation_level = None  # 手动控制，才能用 BEGIN IMMEDIATE 拿写锁
        try:
            conn.execute("BEGIN IMMEDIATE")
            out = fn(conn)
            conn.execute("COMMIT")
            return out
        except sqlite3.OperationalError as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if not is_transient(exc) or attempt == retries - 1:
                raise
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        time.sleep(delay)
        delay = min(delay * 2, 0.3)
    raise RuntimeError("unreachable")


def rows(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    conn = connect()
    try:
        return list(conn.execute(sql, tuple(params)).fetchall())
    finally:
        conn.close()


def one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    conn = connect()
    try:
        return conn.execute(sql, tuple(params)).fetchone()
    finally:
        conn.close()


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    with tx() as conn:
        conn.execute(sql, tuple(params))


SCHEMA = """
-- 访客：凭邀请码进入，没有密码列。user_id 在各表里都指向 visitors.id
CREATE TABLE IF NOT EXISTS visitors (
  id TEXT PRIMARY KEY,
  nickname TEXT NOT NULL,
  invite_code TEXT,
  role TEXT NOT NULL DEFAULT 'visitor',
  pref_effort TEXT NOT NULL DEFAULT 'medium',
  pref_keep_thinking INTEGER NOT NULL DEFAULT 1,
  pref_model TEXT,                       -- 访客的默认模型；NULL = 跟服务端默认
  quota_override INTEGER,
  disabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  last_seen TEXT
);
CREATE INDEX IF NOT EXISTS idx_visitors_created ON visitors(created_at DESC);

-- 邀请码：一码多人用，带次数与到期双约束。
-- daily_quota：该码下每位访客每天可用次数；NULL 表示旧码，迁移时统一回填 50
CREATE TABLE IF NOT EXISTS invites (
  code TEXT PRIMARY KEY,
  max_uses INTEGER NOT NULL DEFAULT 5,
  used INTEGER NOT NULL DEFAULT 0,
  days INTEGER NOT NULL DEFAULT 7,
  daily_quota INTEGER,
  expires_at TEXT NOT NULL,
  note TEXT,
  disabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

-- 会话：kind = visitor | admin；admin 不落 visitors 表
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  ua TEXT,
  ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_subject ON sessions(subject_id);
CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  title TEXT NOT NULL,
  goal TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  stage TEXT NOT NULL DEFAULT 'intake',
  params_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS task_nodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  node_key TEXT NOT NULL,
  seq INTEGER NOT NULL,
  deps TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'todo',
  attempt INTEGER NOT NULL DEFAULT 0,
  input_ref TEXT,
  output_ref TEXT,
  acceptance TEXT,
  version TEXT,
  started_at TEXT,
  ended_at TEXT,
  error TEXT,
  UNIQUE(task_id, node_key)
);
CREATE INDEX IF NOT EXISTS idx_nodes_task ON task_nodes(task_id, seq);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  thinking TEXT,
  signature TEXT,
  stop_reason TEXT,
  tokens_in INTEGER DEFAULT 0,
  tokens_out INTEGER DEFAULT 0,
  cache_read INTEGER DEFAULT 0,
  model TEXT,                            -- 这条回答是哪次模型出的（审计用）
  status TEXT NOT NULL DEFAULT 'done',
  stopped INTEGER NOT NULL DEFAULT 0,   -- 1=用户手动停止且保留了半截正文
  parent_id INTEGER,                     -- P1-5：重生成分支根（被重生成消息或其父）
  branch_index INTEGER,                  -- P1-5：该分支根下第几条分支
  plan_flag INTEGER NOT NULL DEFAULT 0,  -- 1=计划模式产出（只出计划不执行）
  skills TEXT,                           -- 本轮实际注入的技能 id，逗号分隔（审计用）
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id, id);

CREATE TABLE IF NOT EXISTS files (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  task_id TEXT,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  path TEXT NOT NULL,
  mime TEXT,
  size INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT,
  meta_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_task ON files(task_id);
CREATE INDEX IF NOT EXISTS idx_files_user ON files(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS skills (
  id TEXT PRIMARY KEY,
  owner_id TEXT,
  name TEXT NOT NULL,
  version TEXT NOT NULL DEFAULT '1.0.0',
  kind TEXT NOT NULL DEFAULT 'prompt',
  category TEXT NOT NULL DEFAULT '通用',
  enabled INTEGER NOT NULL DEFAULT 1,
  visibility TEXT NOT NULL DEFAULT 'private',
  reviewed_by TEXT,
  is_builtin INTEGER NOT NULL DEFAULT 0,
  published_version TEXT,                -- workflow 技能当前发布版本；NULL=从未发布
  use_count INTEGER NOT NULL DEFAULT 0,  -- 真实使用计数（chat 注入/工作流启动时 +1）
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  skill_id TEXT NOT NULL,
  version TEXT NOT NULL,
  body TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'published',  -- draft | published | archived
  created_by TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skillver ON skill_versions(skill_id, id DESC);

CREATE TABLE IF NOT EXISTS usage_daily (
  visitor_id TEXT NOT NULL,
  day TEXT NOT NULL,
  in_tokens INTEGER NOT NULL DEFAULT 0,
  out_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0,
  calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (visitor_id, day)
);

-- 每次 AI 调用的预扣凭证：先占位再调用，额度判定与并发串行化都靠它
CREATE TABLE IF NOT EXISTS ai_calls (
  request_id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL,
  day TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'reserved',
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  in_tokens INTEGER NOT NULL DEFAULT 0,
  out_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ai_calls_quota ON ai_calls(visitor_id, day, status);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id TEXT,
  action TEXT NOT NULL,
  target TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

-- ---- V4.0 N4 轻量 RAG ----
CREATE TABLE IF NOT EXISTS rag_collections (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  name TEXT NOT NULL,
  desc TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ragcol_user ON rag_collections(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS rag_documents (
  id TEXT PRIMARY KEY,
  collection_id TEXT NOT NULL,
  file_id TEXT,
  name TEXT NOT NULL,
  chars INTEGER NOT NULL DEFAULT 0,
  chunk_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ready',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ragdoc_coll ON rag_documents(collection_id);

CREATE TABLE IF NOT EXISTS rag_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id TEXT NOT NULL,
  collection_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  heading TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,
  tokens INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ragchunk_doc ON rag_chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_ragchunk_coll ON rag_chunks(collection_id);

-- ---- V4.0 N6 工具 ----
CREATE TABLE IF NOT EXISTS tools (
  name TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  desc TEXT NOT NULL DEFAULT '',
  schema_json TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1,
  requires_approval INTEGER NOT NULL DEFAULT 0,
  builtin INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

-- ---- V4.0 N7 记忆 ----
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'note',
  content TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  task_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  change TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_user ON memories(user_id, updated_at DESC);

-- ---- V4.0 N8 制品 ----
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  task_id TEXT,
  message_id INTEGER,
  title TEXT NOT NULL DEFAULT '',
  language TEXT NOT NULL DEFAULT 'html',
  html TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  parent_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifact_task ON artifacts(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_artifact_user ON artifacts(user_id, created_at DESC);

-- ---- V4.0 N9 工作流 ----
CREATE TABLE IF NOT EXISTS workflows (
  id TEXT PRIMARY KEY,
  user_id TEXT,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  builtin INTEGER NOT NULL DEFAULT 0,
  published_version TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_user ON workflows(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS workflow_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL,
  version TEXT NOT NULL,
  graph_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wfver ON workflow_versions(workflow_id, id DESC);

CREATE TABLE IF NOT EXISTS workflow_runs (
  id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  task_id TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  input_json TEXT NOT NULL DEFAULT '{}',
  output_json TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  paused_node TEXT NOT NULL DEFAULT '',
  -- 整图指纹：暂停期间图被改过，resume 会拿着旧输出配新逻辑跑出错误结果。
  -- 存 hash 只为比对，不承载任何内容（agent-flow executionFingerprint 范式）。
  graph_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_wfrun_user ON workflow_runs(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS workflow_run_nodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  node_type TEXT NOT NULL DEFAULT '',
  label TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'todo',
  wave INTEGER NOT NULL DEFAULT 0,
  input_json TEXT NOT NULL DEFAULT '{}',
  output_json TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  started_at TEXT,
  ended_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wfrunnode_run ON workflow_run_nodes(run_id, id);

-- ---- V4.1 插件市场（移植 dsh-market）----
-- 安装状态按用户隔离；manifest_json 是安装时点的目录条目快照（含展开后的 payload）
CREATE TABLE IF NOT EXISTS market_plugins (
  id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  kind TEXT NOT NULL,                -- theme | ui-module | tool-pack
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  installed_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_marketplugin_user ON market_plugins(user_id, kind);

-- 操作审计：每步可撤销。before_json 是前像（安装前为空；主题开关存整组快照）
CREATE TABLE IF NOT EXISTS market_ops (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  op TEXT NOT NULL,                 -- install | uninstall | enable | disable
  plugin_id TEXT NOT NULL,
  before_json TEXT,
  undone INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marketop_user ON market_ops(user_id, created_at DESC);

-- 市场操作意图日志（移植 dsh-market pending-rows）：破坏性步骤之前落 begin，
-- 全部副作用完成后落 done。进程在两者之间被杀时，启动 sweep 按 DB 真相收敛，
-- 不留「DB 已改/注册表未同步」的半截状态（幽灵工具）。
CREATE TABLE IF NOT EXISTS market_journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  op TEXT NOT NULL,                 -- install | uninstall
  plugin_id TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'begin',   -- begin | done
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_marketjournal ON market_journal(user_id, id DESC);
"""


BUILTIN_SKILLS: list[dict[str, str]] = [
    {
        "name": "读书笔记",
        "category": "写作",
        "body": """# 读书笔记方法

## 目标
产出可复用的笔记，不是复述目录。

## 结构
1. 一句话定位：这本书在回答什么问题。
2. 核心命题 3–5 条：每条给原文位置（页码/章节），并用自己的话解释一遍。
3. 论证路径：作者从哪一步推到下一步，哪一步最弱。
4. 与我的关切：只写真正接得上的两点，接不上的不要硬凑。
5. 未解决的问题：留 1–2 个，不要升华式收尾。

## 写作纪律
- 引号内必须是原文，概括不得放进引号。
- 不写"本书深刻揭示了"这类空评价，改用具体判断。
- 段落长短错落，避免通篇等长句。
""",
    },
    {
        "name": "论文精读",
        "category": "研究",
        "body": """# 论文精读方法

## 三步
1. 定位：研究问题是什么，作者认为既有研究错在哪。
2. 拆解：论点、论据、方法、样本、结论，逐项落到具体页码。
3. 评估：结论的适用范围，以及作者没有讨论的反例。

## 引用纪律（硬约束）
- 任何页码、条文、案例都必须来自上传材料，材料里没有就写"材料未见"。
- 不得编造法条编号、案号、文献年份。
- 引用原文与概括分开：引号只装原文。

## 输出格式
- 先用 5 行讲清全文骨架。
- 再按"问题—方法—论证—结论—局限"分节展开。
- 最后给三条可继续追问的问题。
""",
    },
    {
        "name": "汇报大纲",
        "category": "演示",
        "body": """# 汇报大纲方法

## 原则
一页一个判断，标题即结论，不要"背景介绍/研究方法/结论"三件套。

## 结构（默认 6–10 页）
1. 封面：题目 + 一句副标题说明这汇报解决什么。
2. 问题页：为什么现在要讲这件事。
3. 主张页：核心判断一句话。
4. 论证页 2–4 页：每页一个分论点，配一个证据。
5. 反方页：最强的反对意见及回应。
6. 结论页：给出可执行动作。
7. 备注：每页写 60–100 字讲稿。

## 排版约束
- 单页正文不超过 90 字，超过就拆页。
- 标题不超过 18 字。
- 不用装饰性图表，只用能支撑论点的数据。
""",
    },
]


def get_setting(key: str, default: str = "") -> str:
    row = one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---- 轻量迁移 ----
#
# 没有迁移框架，`CREATE TABLE IF NOT EXISTS` 不会给旧库补列。线上库有真实数据，
# 所以这里只做「加可空列」这一种最安全的迁移：
#   - ALTER TABLE ADD COLUMN 在 SQLite 是元数据级操作，毫秒完成、不重写数据；
#   - 先 PRAGMA table_info 判断，天然幂等；
#   - 动手前用官方 backup() 存一份快照，失败可原样覆盖回去；
#   - 失败不阻断启动：缺列时读路径用 .get() 容错、写路径用 has_column() 降级。

_COLUMN_CACHE: dict[tuple[str, str], bool] = {}
MIGRATIONS: dict[str, Any] = {"ok": True, "added": [], "error": ""}


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    key = (table, column)
    if key not in _COLUMN_CACHE:
        try:
            names = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            names = set()
        _COLUMN_CACHE[key] = column in names
    return _COLUMN_CACHE[key]


def has_column(table: str, column: str) -> bool:
    """给路由层用的安全判断：迁移没跑成时返回 False，调用方走降级分支，不抛异常。"""
    conn = connect()
    try:
        return column_exists(conn, table, column)
    except Exception:
        return False
    finally:
        conn.close()


def bump_skill_use(skill_ids: list[str]) -> None:
    """技能真实使用计数：chat 实际注入 / 工作流启动各算一次。

    建任务绑定与启停开关不计——那两处会被空转任务刷数。
    迁移没跑成（列不存在）时静默跳过，不影响主流程。
    """
    ids = [i for i in skill_ids if i]
    if not ids:
        return
    try:
        marks = ",".join("?" * len(ids))
        execute(f"UPDATE skills SET use_count = use_count + 1 WHERE id IN ({marks})", ids)
    except sqlite3.Error:
        pass


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> bool:
    if column_exists(conn, table, column):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()
    _COLUMN_CACHE[(table, column)] = True
    print(f"[migrate] {table}.{column} 已添加", flush=True)
    return True


def _backup_before_migrate(conn: sqlite3.Connection) -> str:
    folder = config.DATA_DIR / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"app-{datetime.now(TZ).strftime('%Y%m%d-%H%M%S')}.db"
    with sqlite3.connect(str(target)) as dst:
        conn.backup(dst)
    return str(target)


def _migrate(conn: sqlite3.Connection) -> None:
    global MIGRATIONS
    MIGRATIONS = {"ok": True, "added": [], "error": ""}
    pending = (
        not column_exists(conn, "visitors", "pref_model")
        or not column_exists(conn, "messages", "model")
        or not column_exists(conn, "invites", "daily_quota")
        or not column_exists(conn, "skills", "published_version")
        or not column_exists(conn, "skill_versions", "status")
        or not column_exists(conn, "skills", "use_count")
        or not column_exists(conn, "messages", "plan_flag")
        or not column_exists(conn, "messages", "skills")
        or not column_exists(conn, "visitors", "pref_web_search")
        or not column_exists(conn, "visitors", "pref_theme")
        or not column_exists(conn, "messages", "web_flag")
        or not column_exists(conn, "messages", "web_sources")
        or not column_exists(conn, "tasks", "workflow_run_id")
        or not column_exists(conn, "tasks", "tool_pending")
        or not column_exists(conn, "messages", "stopped")
        or not column_exists(conn, "messages", "parent_id")
        or not column_exists(conn, "messages", "branch_index")
    )
    try:
        if pending:
            path = _backup_before_migrate(conn)
            print(f"[migrate] 迁移前已备份到 {path}", flush=True)
        if _ensure_column(conn, "visitors", "pref_model", "TEXT"):
            MIGRATIONS["added"].append("visitors.pref_model")
        if _ensure_column(conn, "messages", "model", "TEXT"):
            MIGRATIONS["added"].append("messages.model")
        if _ensure_column(conn, "invites", "daily_quota", "INTEGER"):
            MIGRATIONS["added"].append("invites.daily_quota")
            # 旧码一次性回填：每人每天 50 次（V3.1 起额度由邀请码决定）
            conn.execute("UPDATE invites SET daily_quota = 50 WHERE daily_quota IS NULL")
            conn.commit()
            print("[migrate] invites.daily_quota 旧码已回填 50", flush=True)
        if _ensure_column(conn, "skills", "published_version", "TEXT"):
            MIGRATIONS["added"].append("skills.published_version")
        if _ensure_column(conn, "skill_versions", "status", "TEXT DEFAULT 'published'"):
            MIGRATIONS["added"].append("skill_versions.status")
        if _ensure_column(conn, "skill_versions", "created_by", "TEXT"):
            MIGRATIONS["added"].append("skill_versions.created_by")
        if _ensure_column(conn, "skills", "use_count", "INTEGER NOT NULL DEFAULT 0"):
            MIGRATIONS["added"].append("skills.use_count")
        if _ensure_column(conn, "messages", "plan_flag", "INTEGER NOT NULL DEFAULT 0"):
            MIGRATIONS["added"].append("messages.plan_flag")
        if _ensure_column(conn, "messages", "skills", "TEXT"):
            MIGRATIONS["added"].append("messages.skills")
        # V4.0：联网开关、主题偏好、联网来源留痕、工作流挂接
        if _ensure_column(conn, "visitors", "pref_web_search", "INTEGER NOT NULL DEFAULT 1"):
            MIGRATIONS["added"].append("visitors.pref_web_search")
        if _ensure_column(conn, "visitors", "pref_theme", "TEXT DEFAULT 'light'"):
            MIGRATIONS["added"].append("visitors.pref_theme")
        if _ensure_column(conn, "messages", "web_flag", "INTEGER NOT NULL DEFAULT 0"):
            MIGRATIONS["added"].append("messages.web_flag")
        if _ensure_column(conn, "messages", "web_sources", "TEXT"):
            MIGRATIONS["added"].append("messages.web_sources")
        if _ensure_column(conn, "tasks", "workflow_run_id", "TEXT"):
            MIGRATIONS["added"].append("tasks.workflow_run_id")
        if _ensure_column(conn, "tasks", "tool_pending", "TEXT"):
            MIGRATIONS["added"].append("tasks.tool_pending")
        # P1-4：停止保留的半截正文（status='stopped' 的行 stopped=1）
        if _ensure_column(conn, "messages", "stopped", "INTEGER NOT NULL DEFAULT 0"):
            MIGRATIONS["added"].append("messages.stopped")
        # P1-5：重生成分支。parent_id 指向分支根（被重生成的那条 user 消息或
        # 其父消息），连点重生成的若干新消息共享同一 parent_id；branch_index
        # 是该根下第几条分支。两条均可空，老行不填即 NULL。
        if _ensure_column(conn, "messages", "parent_id", "INTEGER"):
            MIGRATIONS["added"].append("messages.parent_id")
        if _ensure_column(conn, "messages", "branch_index", "INTEGER"):
            MIGRATIONS["added"].append("messages.branch_index")
        # P1-9：记忆变更三态。apply_ops 每执行一个操作就把结果写回这一列
        # （added=新增 / updated=更新 / existing=去重命中未写入），记忆页
        # 由此显示徽标；老行 NULL 表示这轮之前写的，不显示徽标。
        if _ensure_column(conn, "memories", "change", "TEXT"):
            MIGRATIONS["added"].append("memories.change")
        if _ensure_column(conn, "workflow_runs", "paused_node", "TEXT"):
            MIGRATIONS["added"].append("workflow_runs.paused_node")
        # S1-4：resume 防陈旧提交——存整图指纹，暂停期间改图则 409
        if _ensure_column(conn, "workflow_runs", "graph_hash", "TEXT NOT NULL DEFAULT ''"):
            MIGRATIONS["added"].append("workflow_runs.graph_hash")
    except sqlite3.Error as exc:
        # 缺列不影响主流程：模型偏好退回默认、消息审计少一列而已
        MIGRATIONS["ok"] = False
        MIGRATIONS["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[migrate] 失败，已降级为默认模型：{exc}", flush=True)


def init_db() -> None:
    config.ensure_dirs()
    conn = connect()
    try:
        conn.execute(f"PRAGMA journal_mode={journal_mode()}")
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate(conn)
        _init_fts(conn)
        _seed_builtin_skills(conn)
        _seed_builtin_workflow(conn)
        _seed_builtin_tools(conn)
        _seed_workflow_defs(conn)
    finally:
        conn.close()


def _seed_builtin_tools(conn: sqlite3.Connection) -> None:
    """把内置工具写进 tools 表（幂等）。

    工具目录的定义在 services/tools/registry.py，这里只做持久化——
    执行器读的是代码里的白名单，数据库只是「启用状态」的存放处。
    """
    from .services.tools.executor import seed_tools

    try:
        seed_tools()
    except sqlite3.Error as exc:
        print(f"[seed] 工具目录写入失败（不影响主流程）：{exc}", flush=True)


def _seed_workflow_defs(conn: sqlite3.Connection) -> None:
    """内置工作流落库（幂等）。V9 的画布直接读这张表。"""
    from .services.workflow.builtin import builtin_workflows

    now = now_iso()
    for spec in builtin_workflows():
        existing = conn.execute(
            "SELECT id FROM workflows WHERE id = ?", (spec["id"],)
        ).fetchone()
        if existing:
            continue
        conn.execute(
            "INSERT INTO workflows(id, user_id, name, description, builtin, published_version, created_at, updated_at) "
            "VALUES(?,'system',?,?,1,'1.0',?,?)",
            (spec["id"], spec["name"], spec["description"], now, now),
        )
        conn.execute(
            "INSERT INTO workflow_versions(workflow_id, version, graph_json, status, created_at) "
            "VALUES(?, '1.0', ?, 'published', ?)",
            (spec["id"], json.dumps(spec["graph"], ensure_ascii=False), now),
        )
    conn.commit()


# ---- V4.0 N4：FTS5 虚表 + 触发器 ----
#
# FTS5 不是所有 SQLite 编译都带（沙箱与部分 Linux 发行版会缺）。启动时实测一次，
# 不可用就写 settings.fts5_enabled=0，检索层自动切到 BM25 兜底——同一个接口，
# 调用方不需要知道自己走的是哪条路。

FTS_AVAILABLE: bool | None = None


def _init_fts(conn: sqlite3.Connection) -> None:
    global FTS_AVAILABLE
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_fts USING fts5("
            "body, heading, content='rag_chunks', content_rowid='id', tokenize='unicode61')"
        )
        # 外部内容表模式：FTS 只存索引，正文在 rag_chunks。
        # 触发器负责两边同步；缺一个就会静默搜不到东西。
        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS rag_ai AFTER INSERT ON rag_chunks BEGIN
              INSERT INTO rag_chunks_fts(rowid, body, heading)
              VALUES (new.id, new.body, new.heading);
            END;
            CREATE TRIGGER IF NOT EXISTS rag_ad AFTER DELETE ON rag_chunks BEGIN
              INSERT INTO rag_chunks_fts(rag_chunks_fts, rowid, body, heading)
              VALUES ('delete', old.id, old.body, old.heading);
            END;
            CREATE TRIGGER IF NOT EXISTS rag_au AFTER UPDATE ON rag_chunks BEGIN
              INSERT INTO rag_chunks_fts(rag_chunks_fts, rowid, body, heading)
              VALUES ('delete', old.id, old.body, old.heading);
              INSERT INTO rag_chunks_fts(rowid, body, heading)
              VALUES (new.id, new.body, new.heading);
            END;
            """
        )
        conn.commit()
        FTS_AVAILABLE = True
        set_setting("fts5_enabled", "1")
    except sqlite3.Error as exc:
        FTS_AVAILABLE = False
        set_setting("fts5_enabled", "0")
        print(f"[fts5] 不可用，检索走 BM25 兜底：{exc}", flush=True)


def fts5_enabled() -> bool:
    """FTS5 是否可用。settings 表是跨进程的事实源（启动检测写进去）。"""
    global FTS_AVAILABLE
    if FTS_AVAILABLE is not None:
        return FTS_AVAILABLE
    try:
        row = one("SELECT value FROM settings WHERE key = 'fts5_enabled'")
        FTS_AVAILABLE = bool(row and row["value"] == "1")
    except sqlite3.Error:
        FTS_AVAILABLE = False
    return FTS_AVAILABLE


def _seed_builtin_skills(conn: sqlite3.Connection) -> None:
    existing = {r["name"] for r in conn.execute("SELECT name FROM skills WHERE is_builtin = 1")}
    for spec in BUILTIN_SKILLS:
        if spec["name"] in existing:
            continue
        sid = new_id("sk_")
        conn.execute(
            "INSERT INTO skills(id, owner_id, name, version, kind, category, enabled, "
            "visibility, reviewed_by, is_builtin, created_at) "
            "VALUES(?, NULL, ?, '1.0.0', 'prompt', ?, 1, 'shared', 'system', 1, ?)",
            (sid, spec["name"], spec["category"], now_iso()),
        )
        conn.execute(
            "INSERT INTO skill_versions(skill_id, version, body, meta_json, created_at) "
            "VALUES(?, '1.0.0', ?, ?, ?)",
            (sid, spec["body"], json.dumps({"builtin": True}, ensure_ascii=False), now_iso()),
        )
    conn.commit()


# 内置 PPT 工作流：六阶段节点（plan→research→content→render→validate→deliver），
# 渲染前在 content 之后等人工确认大纲（policy.approval_after）。
# meta 必须能通过 skill_tools.validate_workflow_config，发布态直接建成 version='1.0'。
PPT_WORKFLOW_META: dict = {
    "name": "PPT 制作工作流",
    "version": "1.0",
    "description": "六阶段 PPT 生产流程：拆纲 → 资料检索 → 逐页内容 → 渲染 PPTX → 校验 → 交付；渲染前等待人工确认大纲。",
    "nodes": [
        {"key": "plan", "label": "拆纲", "deps": [], "tools": ["minimax.generate"], "retries": 2},
        {"key": "research", "label": "资料检索", "deps": ["plan"],
         "tools": ["file.read", "reference.verify", "minimax.generate"], "retries": 2},
        {"key": "content", "label": "逐页内容", "deps": ["plan", "research"],
         "tools": ["minimax.generate"], "retries": 2},
        {"key": "render", "label": "渲染 PPTX", "deps": ["content"],
         "tools": ["pptx.create", "artifact.write"], "retries": 1},
        {"key": "validate", "label": "校验", "deps": ["render"],
         "tools": ["pptx.validate", "minimax.generate"], "retries": 2},
        {"key": "deliver", "label": "交付", "deps": ["validate"],
         "tools": ["artifact.write"], "retries": 0},
    ],
    "permissions": {
        "file.read": True,
        "reference.verify": True,
        "minimax.generate": True,
        "pptx.create": True,
        "pptx.validate": True,
        "artifact.write": True,
    },
    "params_schema": {
        "page_count": {"type": "integer", "min": 3, "max": 40, "default": 10},
        "topic": {"type": "string"},
    },
    "policy": {
        "approval_after": "content",
        "max_retries_per_step": 2,
        "on_validate_fail": "retry_content",
    },
}

PPT_WORKFLOW_BODY: str = """# PPT 制作工作流（内置 v1.0）

按六个固定节点推进：**拆纲 plan → 资料检索 research → 逐页内容 content → 渲染 PPTX render → 校验 validate → 交付 deliver**。
在 content 完成后暂停，把大纲交给用户确认（approval_after=content），确认前不渲染文件。

## 拆纲（plan）
1. 从任务目标里提取主题、受众、页数（缺省 10 页，3–40 页之间）。
2. 输出三段式大纲：封面 + 目录 + 若干章节页；章节页数量 = 页数 - 2。
3. 每章给出一句「本章要回答的问题」，防止内容页写成名词罗列。

## 资料检索（research）
1. 只使用用户上传的材料（file.read / reference.verify），不编造外部事实。
2. 为每章标注材料出处（文件名 + 页码/段落），找不到材料的章节在大纲里标「待补」。

## 逐页内容（content）
1. 每页输出结构化 JSON：title、bullets（3–5 条，每条 ≤ 22 字）、speaker_notes（80–150 字讲稿）。
2. 讲稿写进 speaker_notes，不堆在页面上；页面正文只保留 bullets。
3. 数字必须来自材料原文，写不出出处就删掉数字。
4. 输出完成后停下等用户确认大纲，用户回复确认或修改意见后再进入渲染。

## 渲染（render）
1. 用 pptx.create 生成可编辑 PPTX：封面页、目录页、章节页按同一套版式。
2. 标题 24pt 以上、正文 14pt 以上；中文字体用思源黑体/微软雅黑。
3. 成果登记到任务交付物（artifact.write），文件名 = 任务标题 + 版本号。

## 校验（validate）
1. 用 pptx.validate 检查：页数与大纲一致、每页 bullets 非空、speaker_notes 已写入。
2. 校验失败触发 content 重跑（只重跑内容，不重跑拆纲与检索）。

## 交付（deliver）
1. 汇总校验报告与文件信息，输出交付卡片。
2. 明确告知用户：修改意见可以走「局部重试」，只重跑对应节点，不扣整次额度。
"""


def _seed_builtin_workflow(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT id FROM skills WHERE kind = 'workflow' AND is_builtin = 1"
    ).fetchone()
    if existing:
        return
    sid = new_id("skw_")
    conn.execute(
        "INSERT INTO skills(id, owner_id, name, version, kind, category, enabled, "
        "visibility, reviewed_by, is_builtin, published_version, created_at) "
        "VALUES(?, 'system', ?, '1.0', 'workflow', '办公', 1, 'shared', 'system', 1, '1.0', ?)",
        (sid, PPT_WORKFLOW_META["name"], now_iso()),
    )
    conn.execute(
        "INSERT INTO skill_versions(skill_id, version, body, meta_json, status, created_by, created_at) "
        "VALUES(?, '1.0', ?, ?, 'published', 'system', ?)",
        (sid, PPT_WORKFLOW_BODY, json.dumps(PPT_WORKFLOW_META, ensure_ascii=False), now_iso()),
    )
    conn.commit()
    print(f"[seed] 内置 PPT 工作流技能已就位 {sid}", flush=True)


def add_audit(actor_id: str | None, action: str, target: str = "", detail: dict | None = None) -> None:
    execute(
        "INSERT INTO audit_log(actor_id, action, target, detail_json, created_at) VALUES(?,?,?,?,?)",
        (actor_id, action, target, json.dumps(detail or {}, ensure_ascii=False), now_iso()),
    )
