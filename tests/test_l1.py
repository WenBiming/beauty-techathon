import json

import pytest

from agent import l1, llm, prompts
from etl import db


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    yield c
    c.close()


class StubClient:
    """按顺序吐出预设文本，记录收到的调用。"""

    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []

    def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
        self.calls.append((model, system, user))
        return llm.LLMResponse(text=self.texts.pop(0), model=model,
                               tokens_in=100, tokens_out=20)


GOOD = json.dumps({
    "scene_minor": "退款迟迟不到账", "confidence": 0.9, "emotion": 2,
    "summary": "买家反馈退款一周未到账", "risk_tags": ["退款时效投诉风险"],
    "promises": [{"text": "若3个工作日内仍未到账，我们走线下打款直接补给您",
                  "amount": 3, "unit": "business_day"}],
}, ensure_ascii=False)


def test_system_prompt_lists_all_41_scenes(conn):
    minors = [r["scene_minor"] for r in conn.execute(
        "SELECT scene_minor FROM scene_map ORDER BY scene_minor")]
    assert len(minors) == 41
    sys = prompts.L1_SYSTEM(minors)
    for m in minors:
        assert m in sys
    assert "scene_major" not in sys, "只让模型预测 minor，不要提 major"


def test_render_dialogue_includes_roles(conn):
    rows = conn.execute(
        "SELECT role, message_text FROM chat WHERE session_id='S00005' ORDER BY sent_at"
    ).fetchall()
    text = prompts.render_dialogue(rows)
    assert "买家:" in text and "客服:" in text
    assert "说好的原路退回呢" in text


def test_analyse_happy_path(conn):
    c = StubClient(GOOD)
    r = l1.analyse(conn, c, "S00005")
    assert r.scene_minor == "退款迟迟不到账"
    assert r.scene_major == "订单服务"          # 由 scene_map 反查，非模型输出
    assert r.emotion == 2
    assert r.degraded is False
    assert r.tokens_in == 100 and r.tokens_out == 20
    assert len(r.promises) == 1
    assert r.promises[0].unit == "business_day"
    assert c.calls[0][0] == "qwen3.8-flash"


def test_analyse_strips_markdown_fence(conn):
    r = l1.analyse(conn, StubClient(f"```json\n{GOOD}\n```"), "S00005")
    assert r.degraded is False
    assert r.scene_minor == "退款迟迟不到账"


def test_analyse_retries_once_then_succeeds(conn):
    c = StubClient("这不是 JSON", GOOD)
    r = l1.analyse(conn, c, "S00005")
    assert r.degraded is False
    assert len(c.calls) == 2


def test_analyse_degrades_after_second_failure(conn):
    c = StubClient("坏输出", "还是坏输出")
    r = l1.analyse(conn, c, "S00005")
    assert r.degraded is True
    assert r.scene_minor == ""
    assert r.emotion == 3                        # 降级为中性
    assert r.promises == []
    assert len(c.calls) == 2


def test_unknown_scene_minor_is_rejected(conn):
    bad = json.dumps({"scene_minor": "我编的场景", "confidence": 0.9, "emotion": 3,
                      "summary": "x", "risk_tags": [], "promises": []},
                     ensure_ascii=False)
    c = StubClient(bad, bad)
    r = l1.analyse(conn, c, "S00005")
    assert r.degraded is True, "白名单外的场景必须判为解析失败"


def test_emotion_out_of_range_is_rejected(conn):
    bad = json.dumps({"scene_minor": "退款迟迟不到账", "confidence": 0.9, "emotion": 9,
                      "summary": "x", "risk_tags": [], "promises": []},
                     ensure_ascii=False)
    r = l1.analyse(conn, StubClient(bad, bad), "S00005")
    assert r.degraded is True
