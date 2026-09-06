"""SQLite 连接与建表。所有列一律 TEXT，保持与 Excel 原值一致（spec §3.2 不做清洗）。

R11：schema 变更对已存在的数据库要生效。派生表（buyer_profile / scene_map /
session_summary / risk_event / promise）的列定义只在 DERIVED_COLUMNS 里维护
一份；DDL 由它生成，_migrate_columns 也读它来给旧库补列，避免两份列清单
互相漂移（这正是本缺陷的成因）。
"""
import sqlite3
from pathlib import Path

from core.config import DB_PATH
from etl.schema import TABLES

# 派生表列定义：表名 -> {列名: 建表用的完整列定义（含约束）}。
# session_summary / risk_event / promise 在 M1 只建表，由 M2 填充。
# UNIQUE 约束不放在这里（SQLite 的 ALTER TABLE 无法给已有表补表内联 UNIQUE），
# 而是作为独立索引写在 INDEX_DDL 里，新库/旧库走同一条路径。
DERIVED_COLUMNS: dict[str, dict[str, str]] = {
    "buyer_profile": {
        "buyer": "TEXT PRIMARY KEY",
        "session_count": "INTEGER NOT NULL",
        "order_count": "INTEGER NOT NULL",
        "total_paid": "REAL NOT NULL",
        "ticket_count": "INTEGER NOT NULL",
        "open_ticket_count": "INTEGER NOT NULL",
        "scene_dist": "TEXT NOT NULL",  # JSON: {scene_major: 次数}
        "first_contact_at": "TEXT",
        "last_contact_at": "TEXT",
        "risk_level": "TEXT",
    },
    "scene_map": {
        "scene_minor": "TEXT PRIMARY KEY",
        "scene_major": "TEXT NOT NULL",
    },
    "session_summary": {
        "session_id": "TEXT PRIMARY KEY",
        "summary": "TEXT",
        "scene_major": "TEXT",
        "scene_minor": "TEXT",
        "intent_confidence": "REAL",
        "emotion": "INTEGER",
        "emotion_trend": "TEXT",  # 上升 / 下降 / 持平 / NULL（无上一次会话）
        "risk_tags": "TEXT",  # JSON 数组
        "suggested_actions": "TEXT",  # JSON 数组（L2 产出）
        "risk_attribution": "TEXT",  # L2 产出：真实风险归因（spec §5.2 卡片③）
        "replies": "TEXT",  # JSON 数组 [{"tone":..,"text":..}]（spec §5.2 卡片④）
        "model": "TEXT",  # JSON 数组：本会话实际用到的模型
        "tokens_in": "INTEGER",  # L1+L2 合计
        "tokens_out": "INTEGER",
        # 分层计量：合计列无法还原单价，成本看板要按模型分别查价目表
        "l1_tokens_in": "TEXT",
        "l1_tokens_out": "TEXT",
        "l2_tokens_in": "TEXT",
        "l2_tokens_out": "TEXT",
        "updated_at": "TEXT",
    },
    "risk_event": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "risk_type": "TEXT NOT NULL",
        "level": "TEXT NOT NULL",
        "session_id": "TEXT",
        "buyer": "TEXT",
        "ticket_no": "TEXT",
        "detected_by": "TEXT NOT NULL",  # L0 / L1 / L2
        "status": "TEXT NOT NULL",  # 待处理 / 跟进中 / 已闭环
        "handler": "TEXT",
        "detail": "TEXT",
        "created_at": "TEXT",
        "updated_at": "TEXT",
    },
    "promise": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "message_id": "TEXT NOT NULL",
        "session_id": "TEXT NOT NULL",
        "buyer": "TEXT NOT NULL",
        "promise_text": "TEXT NOT NULL",
        "promise_type": "TEXT",
        "made_at": "TEXT NOT NULL",
        "deadline_at": "TEXT",
        "ticket_no": "TEXT",
        "closed": "INTEGER NOT NULL DEFAULT 0",
        "overdue": "INTEGER NOT NULL DEFAULT 0",
    },
}


def _build_derived_ddl() -> list[str]:
    ddls = []
    for table, cols in DERIVED_COLUMNS.items():
        col_defs = ", ".join(f"{name} {typ}" for name, typ in cols.items())
        ddls.append(f"CREATE TABLE IF NOT EXISTS {table} ({col_defs})")
    return ddls


DERIVED_DDL = _build_derived_ddl()

INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_chat_session ON chat(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_chat_buyer ON chat(buyer)",
    "CREATE INDEX IF NOT EXISTS idx_orders_buyer ON orders(buyer)",
    "CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(session_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_promise_uniq ON promise(message_id, promise_text)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_risk_event_uniq ON risk_event(risk_type, session_id, detected_by)",
]


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path is not None else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_columns(conn: sqlite3.Connection) -> list[str]:
    """给已物化的派生表补齐 DERIVED_COLUMNS 里新增的列（R11）。

    只处理派生表：原表（TABLES）由 loader 每次全量重灌，不需要迁移。
    ALTER TABLE ADD COLUMN 补的列一律去掉 PRIMARY KEY / NOT NULL / DEFAULT
    等约束，只保留基础类型——SQLite 无法给已有行补 NOT NULL 且无默认值的
    列，派生表的新列本来就该允许 NULL（例如现有的 risk_level TEXT）。
    """
    migrated: list[str] = []
    for table, cols in DERIVED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols.items():
            if name in existing:
                continue
            base_type = typ.split()[0]
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {base_type}")
            migrated.append(f"{table}.{name}")
    if migrated:
        conn.commit()
    return migrated


def create_tables(conn: sqlite3.Connection) -> None:
    for table, spec in TABLES.items():
        cols = []
        for eng in spec.columns.values():
            cols.append(f"{eng} TEXT PRIMARY KEY NOT NULL" if eng == spec.pk else f"{eng} TEXT")
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(cols)})")
    for ddl in DERIVED_DDL:
        conn.execute(ddl)
    _migrate_columns(conn)
    for ddl in INDEX_DDL:
        conn.execute(ddl)
    conn.commit()
