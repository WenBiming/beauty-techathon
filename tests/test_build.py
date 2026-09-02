from core import timeline
from etl import build, db


def test_build_end_to_end(tmp_path):
    """一条命令从 Excel 到可查询的库。"""
    p = tmp_path / "app.db"
    report = build.build(p)
    assert report.session_count == 138
    assert report.buyer_count == 112
    assert report.open_ticket_count == 28
    assert p.is_file()


def test_built_db_supports_timeline_query(tmp_path):
    p = tmp_path / "app.db"
    build.build(p)
    conn = db.connect(p)
    try:
        evs = timeline.buyer_timeline(conn, "魏h**")
        assert len([e for e in evs if e.kind == "chat"]) == 21
        assert any(e.is_open for e in evs if e.kind == "ticket")
    finally:
        conn.close()


def test_build_is_idempotent(tmp_path):
    p = tmp_path / "app.db"
    first = build.build(p)
    second = build.build(p)
    assert first == second
