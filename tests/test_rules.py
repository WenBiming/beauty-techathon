import pytest

from agent import rules
from etl import db


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    yield c
    c.close()


def test_compute_all_covers_every_session(conn):
    all_ = rules.compute_all(conn)
    assert len(all_) == 138


def test_demo_sample_third_contact(conn):
    """S00099：魏h** 第 3 次进线，接入时有 1 张未完结风控工单。"""
    s = rules.compute(conn, "S00099")
    assert s.buyer == "魏h**"
    assert s.prior_session_count == 2
    assert s.open_ticket_count == 1
    assert s.hours_since_prior is not None and s.hours_since_prior > 24


def test_first_contact_has_no_prior(conn):
    s = rules.compute(conn, "S00001")
    assert s.prior_session_count == 0
    assert s.hours_since_prior is None


def test_adverse_reaction_is_redline(conn):
    """S00082 的不良反应工单建单于会话之后，仍必须触发红线。"""
    s = rules.compute(conn, "S00082")
    assert s.has_adverse_reaction is True
    assert s.is_redline is True


def test_exactly_four_sessions_have_open_adverse_ticket(conn):
    """10 张不良反应工单里 4 张未完结，对应 4 个会话。"""
    hits = {k for k, v in rules.compute_all(conn).items() if v.has_adverse_reaction}
    assert hits == {"S00010", "S00058", "S00082", "S00268"}


def test_open_ticket_count_excludes_own_session_ticket(conn):
    """全库 80/80 张工单建单于其会话之后；open_ticket_count 只数此前遗留的。

    S00082 自己产生了一张未完结的不良反应工单，但它不算「此前遗留」。
    """
    s = rules.compute(conn, "S00082")
    assert s.has_adverse_reaction is True
    assert s.open_ticket_count == 0


def test_only_four_sessions_have_prior_open_ticket(conn):
    """情景时钟下仅 4 个会话在接入时已有未闭环工单（spec §4.2.1）。"""
    hits = {k for k, v in rules.compute_all(conn).items() if v.open_ticket_count > 0}
    assert hits == {"S00045", "S00099", "S00102", "S00362"}


def test_repeat_contact_sessions_count(conn):
    """24 个重复进线买家 → 26 个「非首次」会话（22×1 + 2×2）。"""
    n = sum(1 for v in rules.compute_all(conn).values() if v.prior_session_count > 0)
    assert n == 26


def test_response_gap_is_computed(conn):
    s = rules.compute(conn, "S00005")
    assert s.max_response_gap_sec > 0
    assert s.turn_count == 6
