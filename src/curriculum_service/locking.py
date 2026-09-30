"""跨线程/跨进程的文件锁。

发布临界区在 SQLite 事务之外再取一把文件锁，保证多进程同时发布时
也按确定顺序进入仲裁；SQLite 的 ``BEGIN IMMEDIATE`` 与唯一索引负责
最终的原子性兜底。
"""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def file_lock(path: str | os.PathLike[str]) -> Iterator[None]:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
