import pytest

from etl import db, loader

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


def test_open_tickets_total_is_28(conn):
    loader.load_raw_tables(conn)
    total = sum(
        conn.execute(
            f"SELECT COUNT(*) c FROM {t} WHERE status != '已完结'").fetchone()["c"]
        for t in ["ticket_reissue", "ticket_payout", "ticket_logistics",
                  "ticket_adverse", "ticket_return"]
    )
    assert total == 28
