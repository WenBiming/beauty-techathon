import json

import pytest

from agent import tools
from etl import db, derive, loader


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    """写库的测试必须用隔离的临时库。

    连真实 data/app.db 会让 DELETE/UPDATE 洗掉批处理产出——实测发生过：
    跑完全量后再跑一次 pytest，promise 从 181 行掉到 2 行、risk_event 清零。
    ETL 全量重建仅 0.3 秒，module 作用域下每个测试文件只付一次。
    """
    c = db.connect(tmp_path_factory.mktemp("db") / "t.db")
    db.create_tables(c)
    loader.load_raw_tables(c)
    derive.build_all(c)
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


def test_search_cases_returns_same_scene_pairs(conn):
    """search_cases 此前没有任何行为测试，只被 schema 名单点过名。"""
    cases = tools.search_cases(conn, "催发货", k=3)
    assert cases
    assert all(c["scene_minor"] == "催发货" for c in cases)
    assert all(c["buyer_message"] and c["agent_reply"] for c in cases)
    json.dumps(cases, ensure_ascii=False)


def test_search_cases_excludes_current_session(conn):
    """不排除当前会话，就会把 S00099 自己的话术当「历史成功案例」喂回给它（I5）。

    实测：search_cases(conn, "催发货", k=3) 返回 ['S00043','S00062','S00099']，
    而 S00099 正是演示主样本。l2.build_context 一直传了 exclude_session，
    工具路径漏了——同一份知识两条路径只实现了一条。
    """
    plain = [c["session_id"] for c in tools.search_cases(conn, "催发货", k=3)]
    assert "S00099" in plain
    excluded = [c["session_id"] for c in
                tools.search_cases(conn, "催发货", k=3, exclude_session="S00099")]
    assert "S00099" not in excluded
    assert excluded


def test_search_cases_deduplicates_agent_replies(conn):
    """mock 语料有跨 session 逐字重复的客服回复，重复的 few-shot 白花 token（F2）。"""
    scenes = [r["scene_minor"] for r in
              conn.execute("SELECT DISTINCT scene_minor FROM chat")]
    for scene in scenes:
        replies = [c["agent_reply"] for c in tools.search_cases(conn, scene, k=3)]
        assert len(replies) == len(set(replies)), f"{scene} 返回了重复话术"


def test_search_cases_schema_declares_exclude_session():
    sch = next(s for s in tools.TOOL_SCHEMAS
               if s["function"]["name"] == "search_cases")
    assert "exclude_session" in sch["function"]["parameters"]["properties"]


def test_draft_ticket_damage_type_cross_checks_ticket_type(conn):
    """R9：传入图片识别出的语义类型时，用 vision.TICKET_HINT 校验工单类型。"""
    ok = tools.draft_ticket(conn, "S00001", "补发换货", damage_type="broken_pump")
    assert ok["图片识别类型"] == "broken_pump"
    assert ok["图片建议工单类型"] == "补发换货"
    assert ok["图片与工单类型一致"] is True

    mismatch = tools.draft_ticket(conn, "S00001", "补发换货",
                                  damage_type="refund_screenshot")
    assert mismatch["图片建议工单类型"] == "线下打款"
    assert mismatch["图片与工单类型一致"] is False

    plain = tools.draft_ticket(conn, "S00001", "补发换货")
    assert "图片识别类型" not in plain, "不传 damage_type 时草稿结构不变"


def test_draft_ticket_schema_declares_damage_type():
    sch = next(s for s in tools.TOOL_SCHEMAS
               if s["function"]["name"] == "draft_ticket")
    props = sch["function"]["parameters"]["properties"]
    assert "damage_type" in props
    assert "damage_type" not in sch["function"]["parameters"]["required"]


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


def test_check_promises_soft_promise_never_overdue(conn):
    """软承诺（promise_type='soft'）即使 deadline 已过也不算逾期——这条规则
    只存在于 agent.promise.is_overdue_at 里，钉住它能证明 check_promises
    走的是共享实现而不是本地重写的判定逻辑。"""
    conn.execute(
        "INSERT OR REPLACE INTO promise (message_id, session_id, buyer,"
        " promise_text, promise_type, made_at, deadline_at, ticket_no,"
        " closed, overdue) VALUES ('m-test-soft','S00005','魏h**','软承诺测试',"
        " 'soft','2026-05-05 12:08:07','2026-05-06 12:08:07',NULL,0,0)"
    )
    conn.commit()
    try:
        ps = tools.check_promises(conn, "魏h**", as_of="2026-05-09 11:34:05")
        matches = [p for p in ps if p["promise_text"] == "软承诺测试"]
        assert matches and matches[0]["overdue"] is False
    finally:
        conn.execute("DELETE FROM promise WHERE message_id = 'm-test-soft'")
        conn.commit()
