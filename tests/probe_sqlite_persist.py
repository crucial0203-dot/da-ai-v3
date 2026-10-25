"""验证「WAL + 连接反复开关」是不是 readonly 的根因。

D 线程常驻连接（不关闭，-wal/-shm 不会被删）  → 若零失败，假设成立
E 每请求新连接 + 失败重试                     → 看重试能否兜住
F 每请求新连接，但保留一个永不关闭的看门连接    → 折中方案
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent / "data" / "probe3"
FOLDER.mkdir(parents=True, exist_ok=True)
THREADS = 12
ROUNDS = 3


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


def _write(conn: sqlite3.Connection, tag: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT INTO t(k) VALUES(?)", (f"{tag}{time.time_ns()}",))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def d_persistent(path: Path) -> None:
    local = threading.local()

    def get() -> sqlite3.Connection:
        conn = getattr(local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(path), timeout=20.0, check_same_thread=False)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=20000")
            local.conn = conn
        return conn

    _write(get(), "d")


def e_retry(path: Path) -> None:
    for attempt in range(5):
        conn = sqlite3.connect(str(path), timeout=20.0, check_same_thread=False)
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            _write(conn, "e")
            return
        except sqlite3.OperationalError as exc:
            if "readonly" not in str(exc) and "locked" not in str(exc):
                raise
            if attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1))
        finally:
            conn.close()


def f_keeper(path: Path) -> None:
    conn = sqlite3.connect(str(path), timeout=20.0, check_same_thread=False)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=20000")
    try:
        _write(conn, "f")
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

    try:
        for _ in range(ROUNDS):
            ts = [threading.Thread(target=worker) for _ in range(THREADS)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
    except threading.BrokenBarrierError:
        pass

    total = THREADS * ROUNDS
    print(f"  {name:<26} 失败 {len(errors)}/{total}"
          + (f"  → {list(dict.fromkeys(errors))[0]}" if errors else "  → 全通过"))


def main() -> None:
    print(f"[{FOLDER}]  {THREADS} 线程 x {ROUNDS} 轮")
    for name, fn in (
        ("D 线程常驻连接", d_persistent),
        ("E 新连接+重试", e_retry),
        ("F 新连接+看门人(占位)", f_keeper),
    ):
        path = fresh(name.split()[0].lower())
        measure(name, fn, path)


if __name__ == "__main__":
    main()
