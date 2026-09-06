import json

import pytest

from agent import llm


def test_fixture_key_is_stable_and_content_sensitive():
    a = llm.fixture_key("qwen3.8-flash", "sys", "user")
    b = llm.fixture_key("qwen3.8-flash", "sys", "user")
    c = llm.fixture_key("qwen3.8-flash", "sys", "USER")
    assert a == b and a != c
    assert len(a) == 16


def test_fixture_client_replays(tmp_path):
    key = llm.fixture_key("qwen3.8-flash", "S", "U")
    (tmp_path / f"{key}.json").write_text(
        json.dumps({"text": "hello", "model": "qwen3.8-flash",
                    "tokens_in": 10, "tokens_out": 3}),
        encoding="utf-8",
    )
    r = llm.FixtureClient(tmp_path).complete("qwen3.8-flash", "S", "U")
    assert r.text == "hello"
    assert r.tokens_in == 10 and r.tokens_out == 3


def test_fixture_client_raises_on_missing(tmp_path):
    with pytest.raises(llm.FixtureMissing) as e:
        llm.FixtureClient(tmp_path).complete("qwen3.8-flash", "S", "U")
    assert "record_fixtures" in str(e.value)


def test_recording_client_writes_fixture(tmp_path):
    class Fake:
        def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
            return llm.LLMResponse(text="X", model=model, tokens_in=1, tokens_out=2)

    rec = llm.RecordingClient(Fake(), tmp_path)
    rec.complete("qwen3.8-flash", "S", "U")
    key = llm.fixture_key("qwen3.8-flash", "S", "U")
    assert json.loads((tmp_path / f"{key}.json").read_text(encoding="utf-8"))["text"] == "X"
    # 写完之后 FixtureClient 能读回来
    assert llm.FixtureClient(tmp_path).complete("qwen3.8-flash", "S", "U").text == "X"


def test_dashscope_client_disables_thinking(monkeypatch):
    """混合推理模型开着思考链会让 token 上升一个数量级（spec §2.6）。"""
    captured = {}

    class FakeCompletions:
        def create(self, **kw):
            captured.update(kw)
            class U: prompt_tokens = 5; completion_tokens = 6
            class M: content = "ok"
            class C: message = M()
            class R: usage = U(); choices = [C()]
            return R()

    class FakeChat: completions = FakeCompletions()
    class FakeOpenAI:
        def __init__(self, **kw): self.chat = FakeChat()

    monkeypatch.setattr(llm, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    r = llm.DashScopeClient().complete("qwen3.8-flash", "S", "U")
    assert captured["extra_body"] == {"enable_thinking": False}
    assert captured["model"] == "qwen3.8-flash"
    assert r.tokens_in == 5 and r.tokens_out == 6
