import sqlite3

import pytest

from etl import db, derive, loader

BASELINE = {
    "chat": 998, "orders": 113,
    "ticket_reissue": 24, "ticket_payout": 13, "ticket_logistics": 15,
    "ticket_adverse": 10, "ticket_return": 18,
}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.create_tables(c)
    yield c
    c.close()


def test_all_tables_created(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(BASELINE) <= names
    assert {"buyer_profile", "scene_map", "session_summary",
            "risk_event", "promise"} <= names


def test_load_row_counts(conn):
    counts = loader.load_raw_tables(conn)
    assert counts == BASELINE


def test_load_is_idempotent(conn):
    loader.load_raw_tables(conn)
    first = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
             for t in BASELINE}
    loader.load_raw_tables(conn)
    second = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in BASELINE}
    assert first == second == BASELINE


def test_chat_values_preserved_verbatim(conn):
    loader.load_raw_tables(conn)
    row = conn.execute(
        "SELECT * FROM chat WHERE message_id = '20974795108539.PNM'").fetchone()
    assert row["session_id"] == "S00001"
    assert row["sent_at"] == "2026-05-05 10:18:45"
    assert "泵头是坏的" in row["message_text"]


def test_promise_insert_or_replace_is_idempotent(conn):
    """C：promise 需要幂等键，否则批处理重跑会产生重复行并漏进 buyer_timeline。"""
    row = ("MSG-1", "S00001", "魏h**", "72 小时内补发", "补发",
           "2026-05-10 10:00:00", "2026-05-13 10:00:00", None, 0, 0)
    sql = (
        "INSERT OR REPLACE INTO promise (message_id, session_id, buyer,"
        " promise_text, promise_type, made_at, deadline_at, ticket_no,"
        " closed, overdue) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    conn.execute(sql, row)
    conn.execute(sql, row)
    conn.commit()
    count = conn.execute("SELECT COUNT(*) c FROM promise").fetchone()["c"]
    assert count == 1


def test_open_tickets_total_is_28(conn):
    loader.load_raw_tables(conn)
    total = sum(
        conn.execute(
            f"SELECT COUNT(*) c FROM {t} WHERE status != '已完结'").fetchone()["c"]
        for t in ["ticket_reissue", "ticket_payout", "ticket_logistics",
                  "ticket_adverse", "ticket_return"]
    )
    assert total == 28


def test_migrates_missing_column_on_existing_db(tmp_path):
    """R11：schema 变更前就存在的库缺 risk_level 列，create_tables 必须补齐，
    否则 loader/derive 重跑会崩溃在 buyer_profile 的 INSERT 上。"""
    path = tmp_path / "old_buyer_profile.db"
    old_conn = db.connect(path)
    old_conn.execute(
        """
        CREATE TABLE buyer_profile (
            buyer            TEXT PRIMARY KEY,
            session_count    INTEGER NOT NULL,
            order_count      INTEGER NOT NULL,
            total_paid       REAL    NOT NULL,
            ticket_count     INTEGER NOT NULL,
            open_ticket_count INTEGER NOT NULL,
            scene_dist       TEXT    NOT NULL,
            first_contact_at TEXT,
            last_contact_at  TEXT
        )
        """
    )
    old_conn.commit()

    db.create_tables(old_conn)

    cols = {r["name"] for r in old_conn.execute("PRAGMA table_info(buyer_profile)")}
    assert "risk_level" in cols

    # 迁移后重跑 ETL 不应崩溃（此前会抛 OperationalError: no column named risk_level）
    loader.load_raw_tables(old_conn)
    derive.build_all(old_conn)
    old_conn.close()


def test_creates_unique_index_on_existing_db(tmp_path):
    """R11：旧库上的 promise 表没有 UNIQUE 约束时，create_tables 必须补上等价的
    UNIQUE 索引，否则幂等保护（INSERT OR REPLACE 去重）形同虚设。"""
    path = tmp_path / "old_promise.db"
    old_conn = db.connect(path)
    old_conn.execute(
        """
        CREATE TABLE promise (
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
            overdue       INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    old_conn.commit()

    db.create_tables(old_conn)

    row = ("MSG-1", "S00001", "魏h**", "72 小时内补发", "补发",
           "2026-05-10 10:00:00", "2026-05-13 10:00:00", None, 0, 0)
    insert_sql = (
        "INSERT INTO promise (message_id, session_id, buyer, promise_text,"
        " promise_type, made_at, deadline_at, ticket_no, closed, overdue)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    old_conn.execute(insert_sql, row)
    old_conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        old_conn.execute(insert_sql, row)
    old_conn.close()


def test_migrate_is_noop_on_fresh_db(conn):
    """R11：全新库上不该有任何缺列，_migrate_columns 必须是幂等的空操作。"""
    db.create_tables(conn)
    assert db._migrate_columns(conn) == []
