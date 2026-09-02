from datetime import datetime

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


def test_all_buyer_timeline_timestamps_are_isoformat_parseable(conn):
    """spec：预售订单 paid_at 带「（定金）」等中文后缀需在展示层拆掉，
    否则后续里程碑 datetime.fromisoformat(e.ts) 会抛错（全部 112 个买家）。"""
    buyers = [r["buyer"] for r in conn.execute("SELECT DISTINCT buyer FROM chat")]
    assert len(buyers) == 112
    for buyer in buyers:
        for e in timeline.buyer_timeline(conn, buyer):
            datetime.fromisoformat(e.ts)


def test_presale_deposit_order_timestamps_are_clean(conn):
    """三个预售订单付款事件的 ts 应已拆掉「（定金）」备注。"""
    cases = {
        "喵g**": "2026-04-30 09:53:30",
        "不e**": "2026-05-01 14:51:15",
        "姚b**": "2026-05-04 08:00:39",
    }
    for buyer, expected_ts in cases.items():
        evs = timeline.buyer_timeline(conn, buyer)
        paid = [e for e in evs if e.kind == "order" and e.title == "订单付款"
                and e.ts == expected_ts]
        assert paid, f"{buyer} 的预售定金付款事件 ts 未清洗为 {expected_ts}"
        assert "定金" in paid[0].detail


def test_promise_event_appears_when_promise_row_exists(conn):
    """F：promise 表在真实数据里恒空，_promise_events 从未被实测执行过。
    这里手工插一行，验证列名/字段映射正确。"""
    conn.execute(
        "INSERT INTO promise (message_id, session_id, buyer, promise_text,"
        " promise_type, made_at, deadline_at, ticket_no, closed, overdue)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("MSG-TEST-1", "S00005", "魏h**", "72 小时内补发", "补发",
         "2026-05-10 10:00:00", "2026-05-13 10:00:00", None, 0, 1),
    )
    conn.commit()

    evs = timeline.buyer_timeline(conn, "魏h**")
    promises = [e for e in evs if e.kind == "promise"]
    assert len(promises) == 1
    p = promises[0]
    assert p.is_open is True
    assert p.ref_id == "MSG-TEST-1"
    assert "72 小时内补发" in p.detail
    assert "期限 2026-05-13 10:00:00" in p.detail
    assert "已逾期" in p.title
