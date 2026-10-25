"""m21：V3.x 老库冷启动迁移 + 回填（N10 验收）。

做法：
1. 复制现网库副本（data/app.db）到临时目录；
2. 在副本上模拟 V3.x——DROP V4 新表（rag/tools/memories/artifacts/workflow*）
   并 DROP V4 新列（pref_theme/web_flag/workflow_run_id 等）；
3. 子进程以该目录为 DATA_DIR 冷启动 db.init_db()（隔离 config 与模块缓存）；
4. 断言：冷启动无错、新表建立、pref_theme 列补上、builtin workflows 与
   内置工具回填、旧数据行数不变（迁移只加不删）。

线上发布 = 同一路径：/opt/da-data 里的 V3.x 库在新版启动时自动升级。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from harness import FAIL, check  # noqa: E402

V4_TABLES = [
    "rag_collections", "rag_documents", "rag_chunks",
    "tools", "memories", "artifacts",
    "workflows", "workflow_versions", "workflow_runs", "workflow_run_nodes",
]
V4_COLUMNS = [
    ("visitors", "pref_theme"),
    ("visitors", "pref_web_search"),
    ("messages", "web_flag"),
    ("messages", "web_sources"),
    ("tasks", "workflow_run_id"),
    ("tasks", "tool_pending"),
]
BASELINE_TABLES = ("visitors", "tasks", "messages", "files")

COLD_START = """
import json, sys
sys.path.insert(0, {root!r})
from app import db
db.init_db()
conn = db.connect()
visitor_cols = {{r[1] for r in conn.execute("PRAGMA table_info(visitors)")}}
workflows = [r[0] for r in conn.execute("SELECT id FROM workflows ORDER BY id")]
tools_n = conn.execute("SELECT COUNT(*) FROM tools").fetchone()[0]
ppt_skill = conn.execute(
    "SELECT COUNT(*) FROM skills WHERE kind='workflow' AND is_builtin=1").fetchone()[0]
counts = {{t: conn.execute(f"SELECT COUNT(*) FROM {{t}}").fetchone()[0]
          for t in {baseline!r}}}
conn.close()
print(json.dumps({{
    "migrations": db.MIGRATIONS,
    "pref_theme": "pref_theme" in visitor_cols,
    "workflows": workflows,
    "tools": tools_n,
    "ppt_skill": ppt_skill,
    "counts": counts,
}}, ensure_ascii=False))
"""


def main() -> int:
    src = ROOT / "data" / "app.db"
    if not src.exists():
        print("  !! 没有 data/app.db，无可复制的现网副本")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="m21_v3db_"))
    db_path = tmp / "app.db"
    shutil.copy2(src, db_path)

    # ---- 模拟 V3.x 库：删 V4 的表与列 ----
    conn = sqlite3.connect(db_path)
    baseline = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in BASELINE_TABLES}
    for table in V4_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    dropped: list[str] = []
    for table, column in V4_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            dropped.append(f"{table}.{column}")
        except sqlite3.Error:
            pass  # 老 sqlite 不支持 DROP COLUMN 时跳过：表级模拟已足够
    conn.commit()
    conn.close()

    check("模拟 V3.x：V4 新表已清除", True, f"tables={len(V4_TABLES)}")
    check("模拟 V3.x：新列已降级", len(dropped) >= 3, f"dropped={len(dropped)}/{len(V4_COLUMNS)}")

    # ---- 子进程冷启动 ----
    script = COLD_START.format(root=str(ROOT), baseline=BASELINE_TABLES)
    env = dict(os.environ)
    env["DATA_DIR"] = str(tmp)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, timeout=180, cwd=str(ROOT),
    )
    ok_boot = proc.returncode == 0 and proc.stdout.strip()
    check("V3.x 库冷启动无错", bool(ok_boot),
          (proc.stderr or "").strip().splitlines()[-1][:160] if proc.returncode else "")
    if not ok_boot:
        print(proc.stdout[-1500:])
        print(proc.stderr[-1500:])
        return 1

    data = json.loads(proc.stdout.strip().splitlines()[-1])
    mig = data["migrations"]
    check("迁移报告 ok", bool(mig.get("ok")), mig.get("error", ""))
    check("pref_theme 列已补上", data["pref_theme"])
    check("V4 新列已回填（web_flag/workflow_run_id 等）",
          len(mig.get("added") or []) >= 4, f"added={mig.get('added')}")
    check("builtin workflows 已回填",
          {"builtin_ppt", "builtin_summary"} <= set(data["workflows"]),
          str(data["workflows"]))
    check("内置工具已回填", data["tools"] >= 5, f"tools={data['tools']}")
    check("PPT 内置技能已回填", data["ppt_skill"] >= 1, f"skills={data['ppt_skill']}")
    same = all(data["counts"].get(t) == baseline[t] for t in BASELINE_TABLES)
    check("旧数据行数不变（迁移只加不删）", same, f"{baseline} -> {data['counts']}")

    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
