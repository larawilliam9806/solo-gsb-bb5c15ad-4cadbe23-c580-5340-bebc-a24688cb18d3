"""SQLite 数据访问层：连接管理与建表初始化。"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", "data/replay.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS flows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    version     TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id         INTEGER NOT NULL REFERENCES flows(id) ON DELETE CASCADE,
    seq             INTEGER,
    event_type      TEXT NOT NULL,
    step_id         TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}',
    event_time      TEXT NOT NULL,   -- 事件发生的业务时间（客户端提供，可乱序到达）
    received_at     TEXT NOT NULL,   -- 服务端接收时间（到达顺序）
    target_event_id INTEGER          -- undo 事件指向的目标事件 id
);

CREATE INDEX IF NOT EXISTS idx_events_flow ON events(flow_id);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
