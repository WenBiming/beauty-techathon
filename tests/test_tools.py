import json

import pytest

from agent import tools
from etl import db


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    yield c
    c.close()


def test_six_tool_schemas_declared():
    names = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
    assert names == {"get_buyer_timeline", "get_order", "list_open_tickets",
                     "search_cases", "draft_ticket", "check_promises"}
    for s in tools.TOOL_SCHEMAS:
        assert s["type"] == "function"
        assert s["function"]["description"]
        json.dumps(s)                       # 必须可序列化


def test_get_buyer_timeline_is_json_serialisable(conn):
    evs = tools.get_buyer_timeline(conn, "魏h**")
    assert evs and json.dumps(evs, ensure_ascii=False)
    assert {"ts", "kind", "title", "detail", "is_open"} <= set(evs[0])


def test_get_order_returns_row(conn):
    o = tools.get_order(conn, "6920223542160114724")
    assert o is not None
    assert o["buyer"] == "魏h**"
    assert o["province"] == "福建省"


def test_get_order_unknown_returns_none(conn):
    assert tools.get_order(conn, "不存在的订单号") is None


def test_list_open_tickets_respects_as_of(conn):
    """魏h** 的风控工单 05-07 18:15 创建；05-06 时点还看不到它。"""
    assert tools.list_open_tickets(conn, "魏h**", as_of="2026-05-06 00:00:00") == []
    later = tools.list_open_tickets(conn, "魏h**", as_of="2026-05-09 11:34:05")
    assert [t["ticket_no"] for t in later] == ["KOC7263722"]
    assert later[0]["age_days"] >= 1


def test_draft_ticket_prefills_fields(conn):
    d = tools.draft_ticket(conn, "S00001", "补发换货")
    assert d["会话ID"] == "S00001"
    assert d["买家昵称"] == "邓e**"
    assert d["关联订单号"] == "6920185815517983396"
    assert "工单类型" in d


def test_draft_ticket_reissue_has_no_generic_tracking_no(conn):
    """ticket_reissue 拆成 orig/reissue 两个物流号，不能写通用 tracking_no。"""
    d = tools.draft_ticket(conn, "S00001", "补发换货")
    assert "原订单物流单号" in d
    assert "tracking_no" not in d


def test_check_promises_reads_promise_table(conn):
    conn.execute(
        "INSERT OR REPLACE INTO promise (message_id, session_id, buyer,"
        " promise_text, promise_type, made_at, deadline_at, ticket_no,"
        " closed, overdue) VALUES ('m-test','S00005','魏h**','测试承诺','hard',"
        " '2026-05-05 12:08:07','2026-05-08 12:08:07',NULL,0,1)"
    )
    conn.commit()
    try:
        ps = tools.check_promises(conn, "魏h**", as_of="2026-05-09 11:34:05")
        assert any(p["promise_text"] == "测试承诺" and p["overdue"] for p in ps)
    finally:
        conn.execute("DELETE FROM promise WHERE message_id = 'm-test'")
        conn.commit()
