"""SQLite 访问层：连接、建表迁移与基础 CRUD。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import DB_PATH, ensure_dirs

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    version    TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    pid      INTEGER NOT NULL REFERENCES processes(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,
    ts       TEXT NOT NULL,
    kind     TEXT NOT NULL CHECK (kind IN ('add','update','remove')),
    step_key TEXT NOT NULL,
    title    TEXT NOT NULL DEFAULT '',
    data     TEXT NOT NULL DEFAULT '{}',
    revoked  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (pid, seq)
);

CREATE INDEX IF NOT EXISTS idx_events_pid_ts ON events (pid, ts, seq);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """打开一个 SQLite 连接（每行以 dict 形式访问）。"""
    path = Path(db_path) if db_path is not None else DB_PATH
    ensure_dirs()
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """建表并记录 schema 版本（幂等，容器启动时执行）。"""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------- 序列化辅助 ----------------

def _load_data(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def event_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "pid": row["pid"],
        "seq": row["seq"],
        "ts": row["ts"],
        "kind": row["kind"],
        "step_key": row["step_key"],
        "title": row["title"],
        "data": _load_data(row["data"]),
        "revoked": bool(row["revoked"]),
        "created_at": row["created_at"],
    }


def process_to_dict(row: sqlite3.Row, event_count: int | None = None,
                    last_ts: str | None = None) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "name": row["name"],
        "version": row["version"],
        "note": row["note"],
        "created_at": row["created_at"],
    }
    if event_count is not None:
        out["event_count"] = event_count
        out["last_ts"] = last_ts
    return out


# ---------------- 查询 ----------------

def list_processes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT p.*, COUNT(e.id) AS event_count,
               (SELECT MAX(ts) FROM events WHERE pid = p.id) AS last_ts
        FROM processes p LEFT JOIN events e ON e.pid = p.id
        GROUP BY p.id ORDER BY p.created_at DESC, p.id DESC
        """
    ).fetchall()
    return [process_to_dict(r, r["event_count"], r["last_ts"]) for r in rows]


def get_process(conn: sqlite3.Connection, pid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM processes WHERE id = ?", (pid,)).fetchone()


def find_process(conn: sqlite3.Connection, name: str, version: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM processes WHERE name = ? AND version = ?", (name, version)
    ).fetchone()


def list_events(conn: sqlite3.Connection, pid: int, include_revoked: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM events WHERE pid = ?"
    if not include_revoked:
        sql += " AND revoked = 0"
    sql += " ORDER BY seq ASC"
    rows = conn.execute(sql, (pid,)).fetchall()
    return [event_to_dict(r) for r in rows]


def get_event(conn: sqlite3.Connection, pid: int, event_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM events WHERE id = ? AND pid = ?", (event_id, pid)
    ).fetchone()


# ---------------- 写入 ----------------

def create_process(conn: sqlite3.Connection, name: str, version: str,
                   note: str, created_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO processes(name, version, note, created_at) VALUES (?, ?, ?, ?)",
        (name, version, note, created_at),
    )
    return int(cur.lastrowid)


def append_event(conn: sqlite3.Connection, pid: int, ts: str, kind: str,
                 step_key: str, title: str, data: dict[str, Any],
                 created_at: str, revoked: bool = False) -> dict[str, Any]:
    """追加事件；seq 在事务内按当前最大值 +1 分配（事件晚到/乱序不影响落库）。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        pass  # 外层可能已开启事务
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE pid = ?", (pid,)).fetchone()
    seq = int(row["m"]) + 1
    cur = conn.execute(
        """INSERT INTO events(pid, seq, ts, kind, step_key, title, data, revoked, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pid, seq, ts, kind, step_key, title,
         json.dumps(data or {}, ensure_ascii=False), 1 if revoked else 0, created_at),
    )
    row = conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone()
    return event_to_dict(row)


def set_event_revoked(conn: sqlite3.Connection, event_id: int, revoked: bool) -> None:
    conn.execute("UPDATE events SET revoked = ? WHERE id = ?", (1 if revoked else 0, event_id))


def delete_event(conn: sqlite3.Connection, event_id: int) -> None:
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))


def delete_process(conn: sqlite3.Connection, pid: int) -> None:
    conn.execute("DELETE FROM processes WHERE id = ?", (pid,))
