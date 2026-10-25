"""WAL 下逐个变量试：到底是哪一步把连接打成只读。"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent / "data" / "probe4"
FOLDER.mkdir(parents=True, exist_ok=True)
THREADS = 12


def fresh(name: str) -> Path:
    path = FOLDER / f"{name}.db"
    for p in [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]:
        if p.exists():
            p.unlink()
    init = sqlite3.connect(str(path), timeout=20.0)
    init.execute("PRAGMA journal_mode=WAL")
    init.execute("CREATE TABLE t(k TEXT PRIMARY KEY)")
    init.commit()
    init.close()
    return path


def v1_bare(path: Path) -> None:
    conn = sqlite3.connect(str(path), timeout=20.0)
    try:
        conn.execute("INSERT INTO t(k) VALUES(?)", (f"1{time.time_ns()}",))
        conn.commit()
    finally:
        conn.close()


def v2_manual(path: Path) -> None:
    conn = sqlite3.connect(str(path), timeout=20.0)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t(k) VALUES(?)", (f"2{time.time_ns()}",))
        conn.execute("COMMIT")
    finally:
        conn.close()


def v3_sync(path: Path) -> None:
    conn = sqlite3.connect(str(path), timeout=20.0)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t(k) VALUES(?)", (f"3{time.time_ns()}",))
        conn.execute("COMMIT")
    finally:
        conn.close()


def v4_busy(path: Path) -> None:
    conn = sqlite3.connect(str(path), timeout=20.0)
    conn.execute("PRAGMA busy_timeout=20000")
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t(k) VALUES(?)", (f"4{time.time_ns()}",))
        conn.execute("COMMIT")
    finally:
        conn.close()


def v5_checkfirst(path: Path) -> None:
    """先读一次再写，看读操作是否影响。"""
    conn = sqlite3.connect(str(path), timeout=20.0)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute("SELECT COUNT(*) FROM t").fetchone()
        conn.execute("INSERT INTO t(k) VALUES(?)", (f"5{time.time_ns()}",))
        conn.commit()
    finally:
        conn.close()


def measure(name: str, fn, path: Path) -> None:
    errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(THREADS)

    def worker() -> None:
        try:
            barrier.wait()
            fn(path)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    print(f"  {name:<30} 失败 {len(errors)}/{THREADS}"
          + (f"  → {list(dict.fromkeys(errors))[0]}" if errors else "  → 全通过"))


def main() -> None:
    for name, fn in (
        ("v1 裸连接+隐式事务", v1_bare),
        ("v2 manual BEGIN IMMEDIATE", v2_manual),
        ("v3 +synchronous=NORMAL", v3_sync),
        ("v4 +busy_timeout", v4_busy),
        ("v5 先SELECT再写+每次设WAL", v5_checkfirst),
    ):
        measure(name, fn, fresh(name.split()[0]))


if __name__ == "__main__":
    main()
