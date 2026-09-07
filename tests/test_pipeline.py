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
    "risk_tags": ["时效风险"], "high_risk": False,
    "promises": [{"text": "您的订单预计48小时内发出", "amount": 48, "unit": "hour"}],
}, ensure_ascii=False)

L2_OK = json.dumps({
    "risk_attribution": "真实风险在上一单未闭环",
    "suggested_actions": ["先兑现承诺"],
    "replies": [{"tone": "安抚", "text": "上次的事我盯着呢"}],
}, ensure_ascii=False)


class _NoClose:
    """透明代理，只吞掉 close()——sqlite3.Connection.close 是只读属性，
    没法直接 monkeypatch，而 main() 一定会在 finally 里关连接。"""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


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


def test_l2_risk_attribution_and_replies_are_persisted(conn):
    """L2 的三项产出必须全部落库（I1）。

    此前只写 suggested_actions，risk_attribution 与 replies 算完就扔——
    spec §5.2 卡片③要 risk_attribution、卡片④要 replies（共情话术一键插入），
    §5.1「AI 只建议不发送」与 §5.5 的演示动线全建立在卡片④上，库里却没有
    任何一列能喂它。
    """
    pipeline.run_batch(conn, RoutingClient(), ["S00099"])
    row = conn.execute(
        "SELECT * FROM session_summary WHERE session_id = 'S00099'").fetchone()
    assert row["risk_attribution"] == "真实风险在上一单未闭环"
    assert json.loads(row["replies"]) == [{"tone": "安抚", "text": "上次的事我盯着呢"}]
    assert json.loads(row["suggested_actions"]) == ["先兑现承诺"]


def test_session_without_l2_writes_empty_replies(conn):
    """未触发 L2 的会话写 NULL / []，不能让下游对着 NULL 猜格式。"""

    class CalmClient:
        def __init__(self):
            self.calls = {"qwen3.8-flash": 0, "qwen3.7-plus": 0}

        def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
            self.calls[model] += 1
            return llm.LLMResponse(json.dumps({
                "scene_minor": "催发货", "confidence": 0.9, "emotion": 5,
                "summary": "平静", "risk_tags": [], "high_risk": False,
                "promises": [],
            }, ensure_ascii=False), model, 500, 60)

    c = CalmClient()
    pipeline.run_batch(conn, c, ["S00002"])
    assert c.calls["qwen3.7-plus"] == 0, "S00002 平静首次进线，不该触发 L2"
    row = conn.execute(
        "SELECT * FROM session_summary WHERE session_id = 'S00002'").fetchone()
    assert row["risk_attribution"] is None
    assert json.loads(row["replies"]) == []
    assert row["l2_tokens_in"] is None
    assert json.loads(row["model"]) == ["qwen3.8-flash"]


def test_model_column_records_every_model_used(conn):
    """model 列写 JSON 数组，并分层记 token（I3）。

    此前写 r1.model 但 tokens_in/out 是 r1+r2 的和：23 行携带混合 token 却标着
    qwen3.8-flash，成本看板按这一列查价目表会把 plus 的量按 flash 单价计，
    成本低估约 3 倍。
    """
    pipeline.run_batch(conn, RoutingClient(), ["S00099"])
    row = conn.execute(
        "SELECT * FROM session_summary WHERE session_id = 'S00099'").fetchone()
    assert json.loads(row["model"]) == ["qwen3.8-flash", "qwen3.7-plus"]
    assert int(row["l1_tokens_in"]) == 586 and int(row["l1_tokens_out"]) == 74
    assert int(row["l2_tokens_in"]) == 1500 and int(row["l2_tokens_out"]) == 300
    assert row["tokens_in"] == 586 + 1500       # 合计列保持不变
    assert row["tokens_out"] == 74 + 300


def test_emotion_trend_backfilled_against_prior_session(conn):
    """emotion_trend 此前写死 None，实测 138 行全空（I2）。

    spec §5.2 卡片①「情绪条 + 较上次会话的趋势箭头」是第一屏不可折叠内容。
    魏h** 三次进线 S00005 -> S00059 -> S00099，同批次跑完必须能比出趋势——
    这也钉住了「所有 L1 跑完之后统一回填」而不是边跑边写。
    """

    class ByEmotion:
        """按 L1 调用顺序依次给 4 / 2 / 2 分（run_batch 按 session_ids 顺序跑）。"""

        def __init__(self):
            self.calls = {"qwen3.8-flash": 0, "qwen3.7-plus": 0}
            self.emotions = [4, 2, 2]

        def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
            self.calls[model] += 1
            if model == "qwen3.7-plus":
                return llm.LLMResponse(L2_OK, model, 1500, 300)
            emotion = self.emotions[self.calls[model] - 1]
            return llm.LLMResponse(json.dumps({
                "scene_minor": "催发货", "confidence": 0.9, "emotion": emotion,
                "summary": "s", "risk_tags": [], "high_risk": False, "promises": [],
            }, ensure_ascii=False), model, 500, 60)

    ids = ["S00005", "S00059", "S00099"]        # 同一买家，时间递增
    pipeline.run_batch(conn, ByEmotion(), ids)
    trends = {r["session_id"]: r["emotion_trend"] for r in conn.execute(
        "SELECT session_id, emotion_trend FROM session_summary"
        " WHERE session_id IN ('S00005','S00059','S00099')")}
    assert trends["S00005"] is None, "第一次进线没有上一次会话可比"
    assert trends["S00059"] == "下降", "4 分 -> 2 分：情绪恶化"
    assert trends["S00099"] == "持平", "2 分 -> 2 分"


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


def test_format_report_savings_arithmetic_is_correct(conn):
    """算术必须有人验（I11）——此前只断言输出里有「¥」「对照」「节省」字样。

    用 RoutingClient 的固定 token 数手算：L1 每次 586/74，L2 每次 1500/300。
    """
    r = pipeline.run_batch(conn, RoutingClient(), ["S00005", "S00099"])
    prices = {"qwen3.8-flash": (0.0003, 0.0006), "qwen3.7-plus": (0.0008, 0.002)}
    l1c = next(l for l in r.layers if l.layer == "L1")
    l2c = next(l for l in r.layers if l.layer == "L2")
    assert l1c.calls == 2 and l2c.calls == 2      # 两个会话都是高风险
    assert (l1c.tokens_in, l1c.tokens_out) == (586 * 2, 74 * 2)
    assert (l2c.tokens_in, l2c.tokens_out) == (1500 * 2, 300 * 2)

    actual = (586 * 2 / 1000 * 0.0003 + 74 * 2 / 1000 * 0.0006
              + 1500 * 2 / 1000 * 0.0008 + 300 * 2 / 1000 * 0.002)
    # 对照：全部 2 个会话都走 plus，按 L2 实测单次均量（1500/300）外推
    baseline = 1500 * 2 / 1000 * 0.0008 + 300 * 2 / 1000 * 0.002
    saved = (1 - actual / baseline) * 100

    text = pipeline.format_report(r, prices=prices)
    assert f"实际成本：¥{actual:.4f}" in text
    assert f"对照（全量走 qwen3.7-plus）：¥{baseline:.4f}" in text
    assert f"节省：{saved:.1f}%" in text


def test_format_report_skips_baseline_when_l2_never_ran():
    """L2 零调用时没有单次均量可外推。打印「对照 ¥0.0000 / 节省 0.0%」
    比不打印更误导（I8）。"""
    r = pipeline.BatchReport(
        total_sessions=3,
        layers=[pipeline.LayerCost("L0", sessions=3),
                pipeline.LayerCost("L1", sessions=3, calls=3,
                                   tokens_in=1000, tokens_out=200),
                pipeline.LayerCost("L2")])
    text = pipeline.format_report(
        r, prices={"qwen3.8-flash": (0.0003, 0.0006),
                   "qwen3.7-plus": (0.0008, 0.002)})
    assert "实际成本" in text
    assert "对照（全量走" not in text
    assert "节省" not in text
    assert "L2 零调用" in text


def test_parse_price_reads_model_in_out():
    assert pipeline.parse_price("qwen3.8-flash:0.0003:0.0006") == (
        "qwen3.8-flash", (0.0003, 0.0006))


def test_parse_price_rejects_bad_spec():
    with pytest.raises(ValueError):
        pipeline.parse_price("qwen3.8-flash:0.0003")
    with pytest.raises(ValueError):
        pipeline.parse_price(":0.1:0.2")


def test_main_accepts_repeated_price_flags(conn, monkeypatch, capsys):
    """--price 是加分项成本对比表的唯一落地入口（I8）。

    没有它，format_report 的整个金额分支只有测试能到达，而 spec §4.5 的
    成本对比表要进 PPT。
    """
    monkeypatch.setattr(pipeline.db, "connect", lambda *a, **k: _NoClose(conn))
    monkeypatch.setattr(pipeline.db, "create_tables", lambda c: None)
    monkeypatch.setattr(pipeline.llm, "DashScopeClient", RoutingClient)

    rc = pipeline.main(["--sessions", "S00005", "S00099",
                        "--price", "qwen3.8-flash:0.0003:0.0006",
                        "--price", "qwen3.7-plus:0.0008:0.002"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "¥" in out and "对照（全量走 qwen3.7-plus）" in out and "节省" in out


def test_main_without_price_reports_tokens_only(conn, monkeypatch, capsys):
    monkeypatch.setattr(pipeline.db, "connect", lambda *a, **k: _NoClose(conn))
    monkeypatch.setattr(pipeline.db, "create_tables", lambda c: None)
    monkeypatch.setattr(pipeline.llm, "DashScopeClient", RoutingClient)

    assert pipeline.main(["--sessions", "S00099"]) == 0
    assert "¥" not in capsys.readouterr().out
