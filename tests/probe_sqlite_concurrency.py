"""定位并发下 'attempt to write a readonly database'。

对比三种连接策略，各自 20 线程同时写同一个库：
  A 现状：每次 connect 都执行 PRAGMA journal_mode=WAL
  B 只在初始化时设一次 WAL，连接里不再碰 journal_mode
  C 在 B 基础上加 busy_timeout 与一次重试

哪个策略零异常，就用哪个改 app/db.py。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TMP = HERE / "data" / "probe"
TMP.mkdir(parents=True, exist_ok=True)


def make(path: Path) -> None:
    if path.exists():
        path.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    conn = sqlite3.connect(str(path), timeout=20.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(k TEXT PRIMARY KEY, v INTEGER)")
    conn.commit()
    conn.close()


def strategy_a(path: Path) -> None:
    conn = sqlite3.connect(str(path), timeout=20.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t(k, v) VALUES(?, 1)", (f"a{time.time_ns()}",))
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def strategy_b(path: Path) -> None:
    conn = sqlite3.connect(str(path), timeout=20.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t(k, v) VALUES(?, 1)", (f"b{time.time_ns()}",))
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def strategy_c(path: Path) -> None:
    conn = sqlite3.connect(str(path), timeout=20.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t(k, v) VALUES(?, 1)", (f"c{time.time_ns()}",))
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def run(name: str, fn, path: Path, threads: int = 20) -> None:
    errors: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            fn(path)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    for round_no in range(3):
        ts = [threading.Thread(target=worker) for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        if errors:
            break
    total = threads * (round_no + 1)
    print(f"{name}: {threads}x{round_no + 1} 次写，失败 {len(errors)}")
    for msg in dict.fromkeys(errors):
        print("    " + msg)


def main() -> None:
    for name, fn in (("A 每次设WAL", strategy_a), ("B 只设一次", strategy_b), ("C busy_timeout", strategy_c)):
        path = TMP / f"{name[0].lower()}.db"
        make(path)
        run(name, fn, path)


if __name__ == "__main__":
    main()
