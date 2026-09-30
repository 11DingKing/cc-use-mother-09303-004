"""SQLite 存储层。

只依赖标准库。所有写操作在服务层的文件锁临界区内、以
``BEGIN IMMEDIATE`` 事务执行；发布槽位的唯一索引是并发仲裁的
最终防线。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS majors (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batches (
    code        TEXT PRIMARY KEY,
    major_code  TEXT NOT NULL REFERENCES majors(code),
    entry_date  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 课程方案版本：草案与发布版本同表，version_no 为 NULL 即草案。
CREATE TABLE IF NOT EXISTS curriculum_versions (
    id              TEXT PRIMARY KEY,
    major_code      TEXT NOT NULL REFERENCES majors(code),
    scope           TEXT NOT NULL CHECK (scope IN ('MAJOR','BATCH')),
    batch_code      TEXT REFERENCES batches(code),
    version_no      INTEGER,
    state           TEXT NOT NULL,
    kind            TEXT NOT NULL,
    supersedes_id   TEXT REFERENCES curriculum_versions(id),
    based_on_id     TEXT REFERENCES curriculum_versions(id),
    title           TEXT NOT NULL,
    content_json    TEXT NOT NULL,
    content_hash    TEXT,
    effective_date  TEXT,
    published_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(major_code, scope, batch_code, version_no)
);

-- 生效槽位：每个 (专业, 批次范围, 生效日) 至多一个合法版本。
CREATE TABLE IF NOT EXISTS effective_slots (
    id              TEXT PRIMARY KEY,
    major_code      TEXT NOT NULL,
    batch_scope     TEXT NOT NULL,           -- '*' 表示专业级，否则招生批次号
    effective_date  TEXT NOT NULL,
    version_id      TEXT NOT NULL REFERENCES curriculum_versions(id),
    content_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(major_code, batch_scope, effective_date)
);

-- 签名（会签阶段逐条收集）。
CREATE TABLE IF NOT EXISTS signatures (
    id              TEXT PRIMARY KEY,
    version_id      TEXT NOT NULL REFERENCES curriculum_versions(id),
    party_code      TEXT NOT NULL,
    signer          TEXT NOT NULL,
    signed_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(version_id, party_code, signer)
);

-- 签署撤回：撤回签名不等于删除历史；旧版本的适用结论不随之改变。
CREATE TABLE IF NOT EXISTS signature_revocations (
    id              TEXT PRIMARY KEY,
    version_id      TEXT NOT NULL REFERENCES curriculum_versions(id),
    party_code      TEXT NOT NULL,
    signer          TEXT NOT NULL,
    reason          TEXT NOT NULL,
    revoked_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS students (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    major_code      TEXT NOT NULL REFERENCES majors(code),
    batch_code      TEXT NOT NULL REFERENCES batches(code),
    enrolled_at     TEXT NOT NULL
);

-- 学生适用性：学生一旦被解析到某发布版本即冻结，永不改派。
CREATE TABLE IF NOT EXISTS student_bindings (
    student_id      TEXT PRIMARY KEY REFERENCES students(id),
    version_id      TEXT NOT NULL REFERENCES curriculum_versions(id),
    resolved_at     TEXT NOT NULL DEFAULT (datetime('now')),
    reason_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL DEFAULT (datetime('now')),
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}'
);
"""


class Repository:
    """薄封装的 SQLite 仓储，只做存取，不含领域规则。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self.path == ":memory:":
            self.conn.execute("PRAGMA journal_mode=MEMORY")
        else:
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 事务 -------------------------------------------------------------
    def begin_immediate(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    # -- 通用 -------------------------------------------------------------
    @staticmethod
    def _dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def insert_event(self, actor: str, action: str, entity_type: str, entity_id: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO events(actor, action, entity_type, entity_id, payload_json) VALUES (?,?,?,?,?)",
            (actor, action, entity_type, entity_id, self._dumps(payload)),
        )

    def events(self, limit: int = 100) -> list[dict]:
        rows = self.query_all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "actor": r["actor"],
                "action": r["action"],
                "entity_type": r["entity_type"],
                "entity_id": r["entity_id"],
                "payload": json.loads(r["payload_json"]),
            }
            for r in rows
        ]
