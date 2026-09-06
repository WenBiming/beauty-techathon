import json

import pytest

from agent import l1, l2, llm, rules
from etl import db


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    yield c
    c.close()


class StubClient:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []

    def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
        self.calls.append((model, system, user))
        return llm.LLMResponse(text=self.texts.pop(0), model=model,
                               tokens_in=500, tokens_out=200)


def _l1(sid, emotion=3, minor="催发货", major="物流服务"):
    return l1.L1Result(session_id=sid, scene_minor=minor, scene_major=major,
                       confidence=0.9, emotion=emotion, summary="s", risk_tags=[],
                       promises=[], model="qwen3.8-flash", tokens_in=0, tokens_out=0,
                       degraded=False)


GOOD = json.dumps({
    "risk_attribution": "表面是催发货，真实风险在上一单未闭环的风控工单",
    "suggested_actions": ["先兑现 05-05 的线下打款承诺", "催单加急标记"],
    "replies": [{"tone": "安抚", "text": "上次退款的事我这边已经盯着了"},
                {"tone": "专业", "text": "您4号的订单我已加急标记"}],
}, ensure_ascii=False)


def test_redline_triggers(conn):
    s = rules.compute(conn, "S00082")            # 不良反应会话
    assert l2.should_trigger(s, _l1("S00082")) is True


def test_low_emotion_triggers(conn):
    s = rules.compute(conn, "S00002")            # 普通售前咨询
    assert l2.should_trigger(s, _l1("S00002", emotion=2)) is True


def test_open_ticket_triggers(conn):
    s = rules.compute(conn, "S00099")            # 接入时有未闭环工单
    assert l2.should_trigger(s, _l1("S00099")) is True


def test_calm_first_contact_does_not_trigger(conn):
    s = rules.compute(conn, "S00002")
    assert l2.should_trigger(s, _l1("S00002", emotion=4)) is False


def test_degraded_l1_still_triggers(conn):
    """L1 降级说明我们看不清这个会话，宁可多花钱也要看清。"""
    bad = l1.L1Result(session_id="S00002", scene_minor="", scene_major="",
                      confidence=0.0, emotion=3, summary="", risk_tags=[],
                      promises=[], model="qwen3.8-flash", tokens_in=0,
                      tokens_out=0, degraded=True)
    assert l2.should_trigger(rules.compute(conn, "S00002"), bad) is True


def test_context_includes_timeline_and_fewshot(conn):
    s = rules.compute(conn, "S00099")
    ctx = l2.build_context(conn, "S00099", s, _l1("S00099"))
    assert "魏h**" in ctx
    assert "历史成功话术" in ctx
    assert "KOC7263722" in ctx, "全轨迹里必须带上未闭环工单"


def test_context_ticket_age_uses_scenario_clock_consistently(conn):
    """头部摘要的 max_ticket_age_days（情景时钟）与【未闭环工单】小节里同一张
    工单的挂起天数必须一致——否则模型拿到两个矛盾数字，无从分辨哪个权威。"""
    s = rules.compute(conn, "S00099")
    ctx = l2.build_context(conn, "S00099", s, _l1("S00099"))
    assert f"最长挂起 {s.max_ticket_age_days} 天" in ctx
    assert f"KOC7263722（ticket_return）状态 处理中，已挂起 {s.max_ticket_age_days} 天" in ctx
    assert "已挂起 15 天" not in ctx, "不应退化成全局时钟算出的天数"


def test_analyse_happy_path(conn):
    s = rules.compute(conn, "S00099")
    c = StubClient(GOOD)
    r = l2.analyse(conn, c, "S00099", s, _l1("S00099"))
    assert r.degraded is False
    assert c.calls[0][0] == "qwen3.7-plus"
    assert len(r.replies) == 2
    assert r.replies[0].tone == "安抚"
    assert r.tokens_in == 500 and r.tokens_out == 200


def test_analyse_degrades_after_second_failure(conn):
    s = rules.compute(conn, "S00099")
    c = StubClient("坏", "还是坏")
    r = l2.analyse(conn, c, "S00099", s, _l1("S00099"))
    assert r.degraded is True
    assert r.replies == []
    assert len(c.calls) == 2
