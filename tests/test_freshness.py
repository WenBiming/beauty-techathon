import pytest

from agent import l1, llm, pipeline, risk, rules
from etl import db, derive, loader


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    """写库的测试必须用隔离的临时库（M2 裁决 R7）。"""
    c = db.connect(tmp_path_factory.mktemp("db") / "t.db")
    db.create_tables(c)
    loader.load_raw_tables(c)
    derive.build_all(c)
    yield c
    c.close()


def _l1(sid, emotion=3):
    """用关键字参数构造，避免字段顺序变动时静默错位。"""
    return l1.L1Result(
        session_id=sid, scene_minor="催发货", scene_major="物流服务",
        confidence=0.9, emotion=emotion, summary="s", risk_tags=[],
        high_risk=False, promises=[], model="qwen3.8-flash",
        tokens_in=0, tokens_out=0, degraded=False,
    )


def test_last_batch_at_column_exists(conn):
    for table in ("promise", "risk_event"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        assert "last_batch_at" in cols, f"{table} 缺 last_batch_at 列"


def test_persist_stamps_batch_at(conn):
    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    risk.persist(conn, events, "2026-09-07T10:00:00Z")
    rows = conn.execute(
        "SELECT DISTINCT last_batch_at FROM risk_event").fetchall()
    assert [r["last_batch_at"] for r in rows] == ["2026-09-07T10:00:00Z"]
    conn.execute("DELETE FROM risk_event")
    conn.commit()


def test_stale_rows_keep_old_stamp_and_supervisor_state(conn):
    """陈旧行必须保留，且主管的处置标记不能丢——只是批次戳变旧。"""
    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    risk.persist(conn, events, "2026-09-06T10:00:00Z")

    row = conn.execute("SELECT * FROM risk_event LIMIT 1").fetchone()
    conn.execute(
        "UPDATE risk_event SET status='已闭环', handler='主管小李' WHERE id=?",
        (row["id"],))
    conn.commit()

    # 第二批只重算其中一部分事件
    subset = [e for e in events if e.session_id == row["session_id"]]
    risk.persist(conn, subset, "2026-09-07T10:00:00Z")

    kept = conn.execute("SELECT * FROM risk_event WHERE id=?",
                        (row["id"],)).fetchone()
    assert kept["status"] == "已闭环", "主管标记被洗掉了"
    assert kept["handler"] == "主管小李"
    assert kept["last_batch_at"] == "2026-09-07T10:00:00Z"

    stale = conn.execute(
        "SELECT COUNT(*) c FROM risk_event WHERE last_batch_at='2026-09-06T10:00:00Z'"
    ).fetchone()["c"]
    assert stale > 0, "陈旧行应当保留而不是被删除"
    conn.execute("DELETE FROM risk_event")
    conn.commit()


def test_current_batch_at_returns_latest(conn):
    conn.execute("DELETE FROM risk_event")
    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    risk.persist(conn, events, "2026-09-06T10:00:00Z")
    risk.persist(conn, events[:3], "2026-09-07T10:00:00Z")
    assert pipeline.current_batch_at(conn) == "2026-09-07T10:00:00Z"
    conn.execute("DELETE FROM risk_event")
    conn.commit()


def test_current_batch_at_none_when_empty(conn):
    conn.execute("DELETE FROM risk_event")
    conn.execute("DELETE FROM promise")
    conn.commit()
    assert pipeline.current_batch_at(conn) is None


def test_run_batch_stamps_both_tables(conn):
    class Stub:
        def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
            import json
            if model == "qwen3.8-flash":
                return llm.LLMResponse(json.dumps({
                    "scene_minor": "催发货", "confidence": 0.9, "emotion": 3,
                    "summary": "买家催发货", "high_risk": False, "risk_tags": [],
                    "promises": [{"text": "您的订单预计48小时内发出",
                                  "amount": 48, "unit": "hour"}],
                }, ensure_ascii=False), model, 100, 20)
            return llm.LLMResponse(json.dumps({
                "risk_attribution": "x", "suggested_actions": ["a"],
                "replies": [{"tone": "安抚", "text": "y"}],
            }, ensure_ascii=False), model, 200, 50)

    pipeline.run_batch(conn, Stub(), ["S00099"])
    stamp = pipeline.current_batch_at(conn)
    assert stamp is not None
    for table in ("promise", "risk_event"):
        n = conn.execute(
            f"SELECT COUNT(*) c FROM {table} WHERE last_batch_at=?",
            (stamp,)).fetchone()["c"]
        assert n > 0, f"{table} 没有被打上本批次戳"
