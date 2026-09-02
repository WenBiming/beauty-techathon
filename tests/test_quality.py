import pytest

from etl import db, derive, loader, quality


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.create_tables(c)
    loader.load_raw_tables(c)
    derive.build_all(c)
    yield c
    c.close()


def test_report_headline_numbers(conn):
    r = quality.check(conn)
    assert r.session_count == 138
    assert r.buyer_count == 112
    assert r.message_count == 998
    assert r.order_count == 113
    assert r.open_ticket_count == 28


def test_orphan_links_are_zero(conn):
    """spec §2.1：工单/订单的会话ID 100% 可回连聊天。"""
    r = quality.check(conn)
    assert r.orphan_order_sessions == []
    assert r.orphan_ticket_sessions == []


def test_sessions_without_order_or_ticket(conn):
    """spec §2.1：25 个纯售前咨询会话，插件需走降级模式。"""
    r = quality.check(conn)
    assert len(r.consult_only_sessions) == 25


def test_nickname_collision_suspects_listed(conn):
    """spec §2.5.3：昵称是唯一身份键，需列出可疑碰撞供人工核对。"""
    r = quality.check(conn)
    assert isinstance(r.collision_suspects, list)
    for s in r.collision_suspects:
        assert set(s) >= {"buyer", "provinces", "session_count"}


def test_format_report_mentions_mock_disclaimer(conn):
    """spec §2.5.4：MOCK 声明必须保留在所有产出物中。"""
    text = quality.format_report(quality.check(conn))
    assert "MOCK" in text
    assert "138" in text and "112" in text
