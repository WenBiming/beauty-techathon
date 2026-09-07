import json

import pytest

from agent import compliance
from etl import db


@pytest.fixture(scope="module")
def conn():
    """只读测试，可直连真实库。"""
    c = db.connect()
    yield c
    c.close()


def test_known_identifiers_covers_all_three_kinds(conn):
    ids = compliance.known_identifiers(conn)
    assert len(ids) == 335
    assert "6920947277927059788" in ids          # 订单号
    assert "YT7696503801519" in ids              # 物流号
    assert "KOC7263722" in ids                   # 工单号


def test_real_identifier_passes(conn):
    r = compliance.check_reply(
        conn, "S00099", "专业",
        "您的退货工单 KOC7263722 我这边正在盯办，有结果第一时间同步您。")
    assert [i for i in r.issues if i.kind == compliance.KIND_FABRICATED_ID] == []
    assert r.blocked is False


def test_fabricated_tracking_number_is_blocked(conn):
    """实测发现过：模型称「顺丰单号 SF12345678」，而真实单号是圆通 YT7667875838478。"""
    r = compliance.check_reply(
        conn, "S00006", "专业",
        "补发的洗发水已经打包完毕，顺丰单号是：SF1234567890，请注意查收。")
    ids = [i for i in r.issues if i.kind == compliance.KIND_FABRICATED_ID]
    assert len(ids) == 1
    assert "SF1234567890" in ids[0].excerpt
    assert ids[0].severity == compliance.SEV_BLOCK
    assert r.blocked is True


def test_fabricated_order_number_is_blocked(conn):
    r = compliance.check_reply(
        conn, "S00056", "专业", "我刚刚调取了订单 1234567890123456789 的财务后台数据。")
    assert r.blocked is True


def test_identifier_detected_when_adjacent_to_chinese(conn):
    """中文紧邻单号是最常见的形态，必须能检出。

    Python 的 \\b 在这里不成立（中文属于 \\w，「单」和「6」之间无边界），
    所以实现用的是前后向断言而不是 \\b。这条测试就是守这个的。
    """
    r = compliance.check_reply(
        conn, "S00056", "专业", "我刚刚调取了订单1234567890123456789的财务后台数据。")
    assert r.blocked is True, "中文紧邻的编造单号没被检出——检查是否误用了 \\b"


def test_short_numbers_are_not_flagged(conn):
    """手机号、金额这类短数字不该误报。"""
    r = compliance.check_reply(
        conn, "S00099", "专业", "订单金额1598元，如需联系请拨13812345678。")
    assert [i for i in r.issues if i.kind == compliance.KIND_FABRICATED_ID] == []


def test_invented_gender_honorific_is_warning(conn):
    """买家昵称是 邓e** 这样的脱敏形式，性别无从得知。"""
    r = compliance.check_reply(
        conn, "S00001", "致歉", "邓女士您好，非常抱歉让您久等了。")
    h = [i for i in r.issues if i.kind == compliance.KIND_HONORIFIC]
    assert len(h) == 1
    assert h[0].severity == compliance.SEV_WARN
    assert "女士" in h[0].excerpt
    assert r.blocked is False, "称谓问题不阻断，客服自己知道对方性别时可以改"


def test_invented_agent_title_is_warning(conn):
    r = compliance.check_reply(conn, "S00001", "致歉", "您好，我是客服主管，这边为您跟进。")
    h = [i for i in r.issues if i.kind == compliance.KIND_HONORIFIC]
    assert h and "主管" in h[0].excerpt


def test_third_party_title_is_not_flagged(conn):
    """客服说「我联系了仓库主管」是合理信息，不是自称身份，不该报警。"""
    r = compliance.check_reply(
        conn, "S00099", "专业", "我已联系仓库主管为您单独锁定库存，并上报给仓储经理加急。")
    assert [i for i in r.issues if i.kind == compliance.KIND_HONORIFIC] == []


def test_new_promise_is_info_and_extracted(conn):
    r = compliance.check_reply(
        conn, "S00099", "专业", "您的订单我已加急标记，48小时内一定发出。")
    p = [i for i in r.issues if i.kind == compliance.KIND_NEW_PROMISE]
    assert len(p) >= 1
    assert p[0].severity == compliance.SEV_INFO
    assert "48小时内" in p[0].excerpt
    assert r.blocked is False


def test_clean_reply_has_no_issues(conn):
    r = compliance.check_reply(
        conn, "S00099", "安抚", "非常理解您着急的心情，我这边帮您盯着仓库进度。")
    assert r.issues == []
    assert r.blocked is False


def test_check_session_reads_replies_column(conn):
    reports = compliance.check_session(conn, "S00099")
    stored = json.loads(conn.execute(
        "SELECT replies FROM session_summary WHERE session_id='S00099'"
    ).fetchone()["replies"])
    assert len(reports) == len(stored)
    assert {r.tone for r in reports} == {x["tone"] for x in stored}


def test_check_session_empty_when_no_replies(conn):
    sid = conn.execute(
        "SELECT session_id FROM session_summary WHERE replies IN ('','[]') LIMIT 1"
    ).fetchone()
    if sid is None:
        pytest.skip("当前库内所有会话都有话术")
    assert compliance.check_session(conn, sid["session_id"]) == []


def test_known_identifiers_works_on_in_memory_db():
    """内存库上也必须能查出标识符——另开连接的实现会在这里静默返回空集合。"""
    import sqlite3 as _sq

    from etl import derive, loader

    mem = _sq.connect(":memory:")
    mem.row_factory = _sq.Row
    db.create_tables(mem)
    loader.load_raw_tables(mem)
    derive.build_all(mem)
    try:
        assert len(compliance.known_identifiers(mem)) == 335
    finally:
        mem.close()


def test_does_not_import_llm():
    """合规校验必须是确定性的，不得依赖模型。"""
    import inspect

    src = inspect.getsource(compliance)
    assert "agent.llm" not in src
    assert "openai" not in src
