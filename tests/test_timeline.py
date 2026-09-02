import pytest

from core import timeline
from etl import db, derive, loader


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.create_tables(c)
    loader.load_raw_tables(c)
    derive.build_all(c)
    yield c
    c.close()


def test_buyer_timeline_is_chronological(conn):
    evs = timeline.buyer_timeline(conn, "魏h**")
    assert evs == sorted(evs, key=lambda e: e.ts)


def test_buyer_timeline_spans_three_sessions(conn):
    """跨会话全轨迹还原是「信息孤岛」的解药（spec §3.4）。"""
    evs = timeline.buyer_timeline(conn, "魏h**")
    chat = [e for e in evs if e.kind == "chat"]
    assert len(chat) == 21
    assert {e.session_id for e in chat} == {"S00005", "S00059", "S00099"}


def test_buyer_timeline_includes_all_three_kinds(conn):
    evs = timeline.buyer_timeline(conn, "魏h**")
    assert {e.kind for e in evs} >= {"chat", "order", "ticket"}


def test_promise_events_absent_until_m2(conn):
    """promise 表在 M1 为空，timeline 必须能安全处理空表（M2 填充后自动出现）。"""
    evs = timeline.buyer_timeline(conn, "魏h**")
    assert [e for e in evs if e.kind == "promise"] == []


def test_open_ticket_flagged(conn):
    """未完结的风控工单必须被标出，这是插件头卡的风险徽章来源。"""
    evs = timeline.buyer_timeline(conn, "魏h**")
    t = [e for e in evs if e.kind == "ticket" and e.ref_id == "KOC7263722"]
    assert t, "未找到风控工单 KOC7263722"
    assert any(e.is_open for e in t)


def test_session_timeline_only_that_session(conn):
    evs = timeline.session_timeline(conn, "S00005")
    assert evs
    assert all(e.session_id == "S00005" for e in evs)


def test_consult_only_session_has_chat_events_only(conn):
    """25 个纯咨询会话无订单无工单，timeline 不应报错，只返回聊天事件。"""
    evs = timeline.session_timeline(conn, "S00002")
    assert evs
    assert {e.kind for e in evs} == {"chat"}
