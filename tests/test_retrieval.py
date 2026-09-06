import pytest

from agent import retrieval
from etl import db


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    yield c
    c.close()


def test_returns_cases_for_known_scene(conn):
    cases = retrieval.search_similar_cases(conn, "破损换货", k=3)
    assert 1 <= len(cases) <= 3
    for c in cases:
        assert c.scene_minor == "破损换货"
        assert c.buyer_message and c.agent_reply


def test_excludes_given_session(conn):
    cases = retrieval.search_similar_cases(conn, "破损换货", k=10,
                                           exclude_session="S00001")
    assert all(c.session_id != "S00001" for c in cases)


def test_unknown_scene_returns_empty(conn):
    assert retrieval.search_similar_cases(conn, "不存在的场景", k=3) == []


def test_respects_k(conn):
    assert len(retrieval.search_similar_cases(conn, "漏发赠品", k=2)) <= 2


def test_reply_follows_buyer_message(conn):
    """返回的必须是「买家问 → 客服答」的相邻配对，不是随机两条。"""
    for c in retrieval.search_similar_cases(conn, "泛红刺痒", k=3):
        rows = conn.execute(
            "SELECT role, message_text FROM chat WHERE session_id = ? ORDER BY sent_at",
            (c.session_id,),
        ).fetchall()
        texts = [r["message_text"] for r in rows]
        i = texts.index(c.buyer_message)
        assert rows[i]["role"] == "买家"
        assert rows[i + 1]["role"] == "客服"
        assert texts[i + 1] == c.agent_reply
