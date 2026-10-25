"""交叉矩阵：定位 readonly 到底是 WAL 特有，还是目录/文件系统特有。

维度：journal_mode ∈ {WAL, DELETE, TRUNCATE} × 目录 ∈ {项目 data/probe, 系统 TEMP}
每组 12 线程同时 BEGIN IMMEDIATE + INSERT。
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
DIRS = {
    "项目data": HERE / "data" / "probe2",
    "系统TEMP": Path(tempfile.gettempdir()) / "da_probe",
}


def one_write(path: Path, mode: str, tag: str) -> None:
    conn = sqlite3.connect(str(path), timeout=20.0, check_same_thread=False)
    try:
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t(k) VALUES(?)", (f"{tag}{time.time_ns()}",))
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def trial(folder: Path, mode: str, threads: int = 12) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"t_{mode.lower()}.db"
    for p in [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]:
        if p.exists():
            p.unlink()
    init = sqlite3.connect(str(path), timeout=20.0)
    init.execute(f"PRAGMA journal_mode={mode}")
    init.execute("CREATE TABLE t(k TEXT PRIMARY KEY)")
    init.commit()
    actual = init.execute("PRAGMA journal_mode").fetchone()[0]
    init.close()

    errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def worker() -> None:
        try:
            barrier.wait()
            one_write(path, mode, "x")
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    print(f"  {mode:<9} 实际={actual:<7} 失败 {len(errors)}/{threads}"
          + (f"  → {list(dict.fromkeys(errors))[0]}" if errors else "  → 全通过"))


def main() -> None:
    for name, folder in DIRS.items():
        print(f"\n[{name}] {folder}")
        for mode in ("WAL", "DELETE", "TRUNCATE"):
            trial(folder, mode)


if __name__ == "__main__":
    main()
