import json

import pytest

from agent import llm, vision
from core.config import DATA_DIR
from etl import db


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    yield c
    c.close()


class StubVision:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []

    def complete_vision(self, model, system, text, image_data_uri, *, max_tokens=500):
        self.calls.append((model, image_data_uri[:30]))
        return llm.LLMResponse(text=self.texts.pop(0), model=model,
                               tokens_in=800, tokens_out=60)


GOOD = json.dumps({"damage_type": "broken_pump",
                   "description": "粉底液泵头断裂，无法按压出液",
                   "suggested_ticket_type": "补发换货"}, ensure_ascii=False)


def test_ten_damage_types_match_image_dirs():
    assert len(vision.DAMAGE_TYPES) == 10
    assert "broken_pump" in vision.DAMAGE_TYPES
    assert "refund_screenshot" in vision.DAMAGE_TYPES


def test_sessions_with_images_finds_29(conn):
    pairs = vision.sessions_with_images(conn)
    assert len(pairs) == 29
    assert all(p.startswith("mock_images/") for _, p in pairs)


def test_encode_image_returns_data_uri():
    path = DATA_DIR / "mock_images" / "broken_pump" / "S00001_03.jpg"
    uri = vision.encode_image(path)
    assert uri.startswith("data:image/jpeg;base64,")
    assert len(uri) > 100


def test_encode_image_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        vision.encode_image(tmp_path / "nope.jpg")


def test_analyse_happy_path(conn):
    c = StubVision(GOOD)
    r = vision.analyse_image(conn, c, "S00001", "mock_images/broken_pump/S00001_03.jpg")
    assert r.degraded is False
    assert r.damage_type == "broken_pump"
    assert r.suggested_ticket_type == "补发换货"
    assert c.calls[0][0] == "qwen3-vl-flash"
    assert r.tokens_in == 800


def test_unknown_damage_type_is_rejected(conn):
    bad = json.dumps({"damage_type": "我编的类型", "description": "x",
                      "suggested_ticket_type": "补发换货"}, ensure_ascii=False)
    r = vision.analyse_image(conn, StubVision(bad, bad), "S00001",
                             "mock_images/broken_pump/S00001_03.jpg")
    assert r.degraded is True


def test_analyse_degrades_after_second_failure(conn):
    c = StubVision("坏", "还是坏")
    r = vision.analyse_image(conn, c, "S00001",
                             "mock_images/broken_pump/S00001_03.jpg")
    assert r.degraded is True
    assert r.damage_type == ""
    assert len(c.calls) == 2


def test_fixture_key_uses_path_not_bytes():
    """图片重新生成后 base64 会变，但语义没变——key 必须用路径。"""
    k1 = vision.vision_fixture_key("qwen3-vl-flash", "mock_images/a/b.jpg")
    k2 = vision.vision_fixture_key("qwen3-vl-flash", "mock_images/a/b.jpg")
    k3 = vision.vision_fixture_key("qwen3-vl-flash", "mock_images/a/c.jpg")
    assert k1 == k2 != k3
