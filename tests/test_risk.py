from datetime import datetime

import pytest

from agent import l1, promise, risk, rules
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


def _l1(session_id, emotion=3, minor="催发货", major="物流服务"):
    return l1.L1Result(session_id=session_id, scene_minor=minor, scene_major=major,
                       confidence=0.9, emotion=emotion, summary="s", risk_tags=[],
                       promises=[], model="qwen3.8-flash", tokens_in=0, tokens_out=0,
                       degraded=False)


def test_six_risk_types_declared():
    assert len(risk.RISK_TYPES) == 6
    assert "不良反应未闭环" in risk.RISK_TYPES
    assert "承诺逾期" in risk.RISK_TYPES


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
