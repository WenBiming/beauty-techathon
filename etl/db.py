"""SQLite 连接与建表。所有列一律 TEXT，保持与 Excel 原值一致（spec §3.2 不做清洗）。"""
import sqlite3
from pathlib import Path

from core.config import DB_PATH
from etl.schema import TABLES

# 派生表 DDL。session_summary / risk_event / promise 在 M1 只建表，由 M2 填充。
DERIVED_DDL = [
    """
    CREATE TABLE IF NOT EXISTS buyer_profile (
        buyer            TEXT PRIMARY KEY,
        session_count    INTEGER NOT NULL,
        order_count      INTEGER NOT NULL,
        total_paid       REAL    NOT NULL,
        ticket_count     INTEGER NOT NULL,
        open_ticket_count INTEGER NOT NULL,
        scene_dist       TEXT    NOT NULL,   -- JSON: {scene_major: 次数}
        first_contact_at TEXT,
        last_contact_at  TEXT,
        risk_level       TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scene_map (
        scene_minor TEXT PRIMARY KEY,
        scene_major TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_summary (
        session_id       TEXT PRIMARY KEY,
        summary          TEXT,
        scene_major      TEXT,
        scene_minor      TEXT,
        intent_confidence REAL,
        emotion          INTEGER,
        emotion_trend    TEXT,
        risk_tags        TEXT,      -- JSON 数组
        suggested_actions TEXT,     -- JSON 数组
        model            TEXT,
        tokens_in        INTEGER,
        tokens_out       INTEGER,
        updated_at       TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS risk_event (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        risk_type   TEXT NOT NULL,
        level       TEXT NOT NULL,
        session_id  TEXT,
        buyer       TEXT,
        ticket_no   TEXT,
        detected_by TEXT NOT NULL,   -- L0 / L1 / L2
        status      TEXT NOT NULL,   -- 待处理 / 跟进中 / 已闭环
        handler     TEXT,
        detail      TEXT,
        created_at  TEXT,
        updated_at  TEXT,
        UNIQUE(risk_type, session_id, detected_by)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS promise (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id    TEXT NOT NULL,
        session_id    TEXT NOT NULL,
        buyer         TEXT NOT NULL,
        promise_text  TEXT NOT NULL,
        promise_type  TEXT,
        made_at       TEXT NOT NULL,
        deadline_at   TEXT,
        ticket_no     TEXT,
        closed        INTEGER NOT NULL DEFAULT 0,
        overdue       INTEGER NOT NULL DEFAULT 0,
        UNIQUE(message_id, promise_text)
    )
    """,
]

INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_chat_session ON chat(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_chat_buyer ON chat(buyer)",
    "CREATE INDEX IF NOT EXISTS idx_orders_buyer ON orders(buyer)",
    "CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(session_id)",
]


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path is not None else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables(conn: sqlite3.Connection) -> None:
    for table, spec in TABLES.items():
        cols = []
        for eng in spec.columns.values():
            cols.append(f"{eng} TEXT PRIMARY KEY NOT NULL" if eng == spec.pk else f"{eng} TEXT")
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(cols)})")
    for ddl in DERIVED_DDL:
        conn.execute(ddl)
    for ddl in INDEX_DDL:
        conn.execute(ddl)
    conn.commit()
