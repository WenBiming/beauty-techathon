from datetime import datetime

import pytest

from agent import l1, promise, risk, rules
from core.clock import reference_now
from etl import db, derive, loader
from etl.schema import CLOSED_STATUS, TICKET_TABLES


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


def _l1(session_id, emotion=3, minor="催发货", major="物流服务"):
    return l1.L1Result(session_id=session_id, scene_minor=minor, scene_major=major,
                       confidence=0.9, emotion=emotion, summary="s", risk_tags=[],
                       high_risk=False,
                       promises=[], model="qwen3.8-flash", tokens_in=0, tokens_out=0,
                       degraded=False)


def test_six_risk_types_all_reachable_from_detect(conn):
    """断言 detect() 的**输出**覆盖六类，而不是拿 RISK_TYPES 列表验证它自己。

    此前这个测试只比对字面量列表，而 RISK_TYPES 在生产代码里从未被引用——
    把 detect() 里内联的字符串改成任何别的值，137 个测试照样全绿（I11）。
    """
    assert len(risk.RISK_TYPES) == 6
    sig = rules.compute_all(conn)
    l1r = {k: _l1(k) for k in sig}
    l1r["S00059"] = _l1("S00059", emotion=1)       # 造出情绪升级
    ps = promise.evaluate(
        conn, "S00005",
        [promise.RawPromise("若3个工作日内仍未到账，我们走线下打款直接补给您",
                            3, "business_day")],
        as_of=datetime(2026, 5, 23, 10, 4, 48),
    )
    produced = {e.risk_type for e in risk.detect(conn, sig, l1r, ps)}
    assert produced == set(risk.RISK_TYPES)


def test_ticket_age_alert_shares_threshold_with_rules():
    """超期天数阈值只有一份定义，在 rules.py（I10）。"""
    assert risk.TICKET_AGE_ALERT_DAYS is rules.REDLINE_TICKET_AGE_DAYS


def test_adverse_reaction_is_red(conn):
    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    adverse = [e for e in events if e.risk_type == "不良反应未闭环"]
    assert adverse, "10 张不良反应工单里有 4 张未完结，必须产出预警"
    assert all(e.level == "红" for e in adverse)
    assert all(e.detected_by == "L0" for e in adverse)


def test_repeat_contact_detected(conn):
    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    repeat = {e.session_id for e in events if e.risk_type == "重复进线"}
    assert "S00099" in repeat
    assert len(repeat) == 26        # 22 个买家各 1 次 + 2 个买家各 2 次


def test_third_contact_is_red_second_is_orange(conn):
    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    by_sess = {e.session_id: e for e in events if e.risk_type == "重复进线"}
    assert by_sess["S00099"].level == "红"       # 第 3 次进线
    assert by_sess["S00059"].level == "橙"       # 第 2 次进线


def test_emotion_escalation_detected_by_l1(conn):
    sig = rules.compute_all(conn)
    l1r = {k: _l1(k) for k in sig}
    l1r["S00059"] = _l1("S00059", emotion=1)
    events = risk.detect(conn, sig, l1r, [])
    esc = [e for e in events if e.risk_type == "情绪升级"]
    assert any(e.session_id == "S00059" for e in esc)
    assert all(e.detected_by == "L1" for e in esc)


def test_overdue_promise_becomes_risk(conn):
    sig = rules.compute_all(conn)
    ps = promise.evaluate(
        conn, "S00005",
        [promise.RawPromise("若3个工作日内仍未到账，我们走线下打款直接补给您",
                            3, "business_day")],
        as_of=datetime(2026, 5, 23, 10, 4, 48),
    )
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, ps)
    over = [e for e in events if e.risk_type == "承诺逾期"]
    assert len(over) == 1
    assert over[0].session_id == "S00005"
    assert over[0].buyer == "魏h**"


def test_ticket_backlog_uses_global_clock(conn):
    """「工单积压超期」必须按**全局时钟**直读工单表算（C2）。

    此前它消费 rules.compute 的 max_ticket_age_days（情景时钟 + own_session
    过滤），全库 80/80 张工单都建单于本会话之后，于是这一类几乎恒为 0，只
    产出 1 条——而 spec §6.1 期望的是 28 张在途。同一张 risk_event 表里承诺
    逾期用全局时钟、工单龄期用情景时钟，本身就不自洽。
    """
    global_now = reference_now(conn)
    expected = set()
    for table in TICKET_TABLES:
        for r in conn.execute(
            f"SELECT ticket_no, created_at FROM {table} WHERE status != ?",
            (CLOSED_STATUS,),
        ):
            created = datetime.fromisoformat(r["created_at"])
            if created <= global_now and (global_now - created).days > 3:
                expected.add(r["ticket_no"])
    assert len(expected) == 27, "全局时钟下龄期 > 3 天的未完结工单实测 27 张"

    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    aged = [e for e in events if e.risk_type == "工单积压超期"]
    assert {e.ticket_no for e in aged} == expected
    assert len(aged) == 27
    assert len({e.buyer for e in aged}) == 25
    assert all(e.level == "橙" and e.detected_by == "L0" for e in aged)
    assert all(e.session_id is not None for e in aged), "session_id 用工单自己的"


def test_repeat_refund_risk_generated(conn):
    """「重复退款风控」有生成测试——六类里此前有两类完全没测（I11）。"""
    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    refund = [e for e in events if e.risk_type == "重复退款风控"]
    assert refund, "累计 2 张及以上退款/退货工单的买家必须出预警"
    assert all(e.level == "橙" and e.detected_by == "L0" for e in refund)
    for e in refund:
        assert sig[e.session_id].refund_ticket_count >= risk.REFUND_ALERT_COUNT


def test_persist_is_idempotent(conn):
    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    first = risk.persist(conn, events)
    before = conn.execute("SELECT COUNT(*) c FROM risk_event").fetchone()["c"]
    risk.persist(conn, events)
    after = conn.execute("SELECT COUNT(*) c FROM risk_event").fetchone()["c"]
    assert first == len(events)
    assert before == after
    conn.execute("DELETE FROM risk_event")
    conn.commit()


def test_persist_preserves_supervisor_state(conn):
    """重跑不能抹掉主管标记的处置状态（C1）。

    行数不变是不够的断言——INSERT OR REPLACE 的语义是「删冲突行再插入」，
    行数照样不变，但 status/handler/created_at 全被重置、id 换新。spec §4.8
    要求演示视频里现场点一次「实时重算」，REPLACE 之下按一次所有已闭环预警
    归零，§6.2 的状态机与闭环率直接失效。
    """
    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    risk.persist(conn, events)

    target = conn.execute(
        "SELECT id, risk_type, session_id, detected_by, created_at"
        " FROM risk_event ORDER BY id LIMIT 1").fetchone()
    conn.execute(
        "UPDATE risk_event SET status = '已闭环', handler = '主管小李' WHERE id = ?",
        (target["id"],),
    )
    conn.commit()

    risk.persist(conn, events)          # 主管标记之后再重算一次

    row = conn.execute(
        "SELECT * FROM risk_event WHERE risk_type = ? AND session_id = ?"
        " AND detected_by = ?",
        (target["risk_type"], target["session_id"], target["detected_by"]),
    ).fetchone()
    assert row["status"] == "已闭环", "重跑把主管标记的状态回退成了待处理"
    assert row["handler"] == "主管小李", "重跑清空了 handler"
    assert row["id"] == target["id"], "重跑换了主键 id，看板的引用会全部失效"
    assert row["created_at"] == target["created_at"], "重跑重置了 created_at"

    conn.execute("DELETE FROM risk_event")
    conn.commit()
