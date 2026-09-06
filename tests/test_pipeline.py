import json

import pytest

from agent import llm, pipeline
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


L1_OK = json.dumps({
    "scene_minor": "催发货", "confidence": 0.9, "emotion": 2, "summary": "买家催发货",
    "risk_tags": ["时效风险"],
    "promises": [{"text": "您的订单预计48小时内发出", "amount": 48, "unit": "hour"}],
}, ensure_ascii=False)

L2_OK = json.dumps({
    "risk_attribution": "真实风险在上一单未闭环",
    "suggested_actions": ["先兑现承诺"],
    "replies": [{"tone": "安抚", "text": "上次的事我盯着呢"}],
}, ensure_ascii=False)


class RoutingClient:
    """按模型名返回不同响应，并记录每层调用次数。"""

    def __init__(self):
        self.calls = {"qwen3.8-flash": 0, "qwen3.7-plus": 0}

    def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
        self.calls[model] += 1
        if model == "qwen3.8-flash":
            return llm.LLMResponse(L1_OK, model, 586, 74)
        return llm.LLMResponse(L2_OK, model, 1500, 300)


def test_run_batch_on_four_demo_sessions(conn):
    c = RoutingClient()
    ids = ["S00005", "S00059", "S00099", "S00362"]
    r = pipeline.run_batch(conn, c, ids)
    assert r.total_sessions == 4
    assert r.l1_degraded == 0
    assert c.calls["qwen3.8-flash"] == 4, "L1 必须全量过一遍"
    assert 0 < c.calls["qwen3.7-plus"] <= 4, "L2 只对高风险触发"
    assert r.l2_triggered == c.calls["qwen3.7-plus"]


def test_l1_layer_tokens_are_accounted(conn):
    r = pipeline.run_batch(conn, RoutingClient(), ["S00005", "S00099"])
    l1_layer = next(l for l in r.layers if l.layer == "L1")
    assert l1_layer.calls == 2
    assert l1_layer.tokens_in == 586 * 2
    assert l1_layer.tokens_out == 74 * 2


def test_l0_layer_is_zero_token(conn):
    r = pipeline.run_batch(conn, RoutingClient(), ["S00005"])
    l0 = next(l for l in r.layers if l.layer == "L0")
    assert l0.tokens_in == 0 and l0.tokens_out == 0
    assert l0.sessions == 1


def test_session_summary_written(conn):
    pipeline.run_batch(conn, RoutingClient(), ["S00099"])
    row = conn.execute(
        "SELECT * FROM session_summary WHERE session_id = 'S00099'").fetchone()
    assert row["scene_minor"] == "催发货"
    assert row["scene_major"] == "物流服务"      # 反查得来
    assert row["emotion"] == 2
    assert json.loads(row["risk_tags"]) == ["时效风险"]
    assert row["tokens_in"] > 0


def test_promises_persisted(conn):
    conn.execute("DELETE FROM promise")
    conn.commit()
    r = pipeline.run_batch(conn, RoutingClient(), ["S00099"])
    assert r.promise_count >= 1
    assert conn.execute("SELECT COUNT(*) c FROM promise").fetchone()["c"] >= 1


def test_run_batch_is_idempotent(conn):
    ids = ["S00005", "S00099"]
    pipeline.run_batch(conn, RoutingClient(), ids)
    n1 = conn.execute("SELECT COUNT(*) c FROM risk_event").fetchone()["c"]
    p1 = conn.execute("SELECT COUNT(*) c FROM promise").fetchone()["c"]
    pipeline.run_batch(conn, RoutingClient(), ids)
    assert conn.execute("SELECT COUNT(*) c FROM risk_event").fetchone()["c"] == n1
    assert conn.execute("SELECT COUNT(*) c FROM promise").fetchone()["c"] == p1


def test_buyer_risk_level_is_persisted(conn):
    """M1 预留的 buyer_profile.risk_level 必须被填上（红 > 橙 > NULL）。"""
    conn.execute("UPDATE buyer_profile SET risk_level = NULL")
    conn.commit()
    pipeline.run_batch(conn, RoutingClient(), ["S00099"])
    row = conn.execute(
        "SELECT risk_level FROM buyer_profile WHERE buyer = '魏h**'").fetchone()
    assert row["risk_level"] in ("红", "橙")


def test_format_report_without_prices_shows_tokens_only(conn):
    r = pipeline.run_batch(conn, RoutingClient(), ["S00005", "S00099"])
    text = pipeline.format_report(r)
    assert "L0" in text and "L1" in text and "L2" in text
    assert "token" in text
    assert "¥" not in text, "没给单价就不该出现金额"


def test_format_report_with_prices_shows_savings(conn):
    r = pipeline.run_batch(conn, RoutingClient(), ["S00005", "S00099"])
    text = pipeline.format_report(
        r, prices={"qwen3.8-flash": (0.0003, 0.0006),
                   "qwen3.7-plus": (0.0008, 0.002)})
    assert "¥" in text
    assert "对照" in text and "节省" in text
