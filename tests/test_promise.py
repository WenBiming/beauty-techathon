from datetime import datetime

import pytest

from agent import promise
from etl import db


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    yield c
    c.close()


def test_resolve_hours():
    made = datetime(2026, 5, 5, 12, 0)
    assert promise.resolve_deadline(made, 48, "hour") == datetime(2026, 5, 7, 12, 0)


def test_resolve_days():
    made = datetime(2026, 5, 5, 12, 0)
    assert promise.resolve_deadline(made, 2, "day") == datetime(2026, 5, 7, 12, 0)


def test_resolve_business_days_skips_weekend():
    """2026-05-05 是周二，+3 个工作日 = 05-08 周五。"""
    made = datetime(2026, 5, 5, 12, 0)
    assert promise.resolve_deadline(made, 3, "business_day") == datetime(2026, 5, 8, 12, 0)
    # 周四 +2 工作日 跨周末 = 下周一
    thu = datetime(2026, 5, 7, 9, 0)
    assert promise.resolve_deadline(thu, 2, "business_day") == datetime(2026, 5, 11, 9, 0)


def test_soft_promise_has_no_deadline():
    assert promise.resolve_deadline(datetime(2026, 5, 5), None, None) is None


def test_locate_message_finds_the_agent_turn(conn):
    row = promise.locate_message(conn, "S00005", "若3个工作日内仍未到账")
    assert row is not None
    assert row["role"] == "客服"
    assert row["sent_at"] == "2026-05-05 12:08:07"


def test_locate_message_returns_none_when_absent(conn):
    assert promise.locate_message(conn, "S00005", "这句话根本不存在于本会话") is None


def test_evaluate_marks_hard_promise_overdue_at_next_contact(conn):
    """魏h** 在 S00005 承诺 3 个工作日；到 05-09 再次进线时已逾期。"""
    raws = [promise.RawPromise("若3个工作日内仍未到账，我们走线下打款直接补给您",
                               3, "business_day")]
    out = promise.evaluate(conn, "S00005", raws, as_of=datetime(2026, 5, 9, 11, 34, 5))
    assert len(out) == 1
    p = out[0]
    assert p.promise_type == "hard"
    assert p.deadline_at == "2026-05-08 12:08:07"
    assert p.buyer == "魏h**"
    assert p.overdue is True
    assert p.closed is False


def test_evaluate_not_overdue_before_deadline(conn):
    raws = [promise.RawPromise("若3个工作日内仍未到账，我们走线下打款直接补给您",
                               3, "business_day")]
    out = promise.evaluate(conn, "S00005", raws, as_of=datetime(2026, 5, 6, 9, 0))
    assert out[0].overdue is False


def test_soft_promise_never_overdue(conn):
    raws = [promise.RawPromise("这边立刻为您核实", None, None)]
    out = promise.evaluate(conn, "S00005", raws, as_of=datetime(2026, 9, 5))
    assert out[0].promise_type == "soft"
    assert out[0].deadline_at is None
    assert out[0].overdue is False


def test_closed_when_session_ticket_finished(conn):
    """S00007 的补发换货工单 BH142092289687 已完结，其承诺应判为已闭环。"""
    raws = [promise.RawPromise("已为您创建补发工单，48小时内发出", 48, "hour")]
    out = promise.evaluate(conn, "S00007", raws, as_of=datetime(2026, 9, 5))
    assert out[0].closed is True
    assert out[0].overdue is False
    assert out[0].ticket_no == "BH142092289687"


def test_open_ticket_leaves_promise_unclosed(conn):
    """S00001 的补发换货工单 BH919209358357 仍是「进行中」，承诺不应判为闭环。"""
    raws = [promise.RawPromise("换货单已创建，48小时内发出", 48, "hour")]
    out = promise.evaluate(conn, "S00001", raws, as_of=datetime(2026, 9, 5))
    assert out[0].closed is False
    assert out[0].overdue is True
    assert out[0].ticket_no == "BH919209358357"


def test_ticket_finished_after_deadline_is_overdue(conn):
    """S00051「预计1小时内送达」deadline 16:52:20，工单 WL840101369 直到
    21:33:03 才完结——晚了 4 小时 41 分。这是**真的没按时兑现**（I6）。

    此前 session_ticket 只看 status 不看完结时间，is_overdue_at 一旦 closed
    为真就返回 False，于是这类假阴性恰好发生在本作品的招牌能力「隐性服务
    风险」上：全库 58 条 hard+closed 承诺里有 7 条属于这一类。
    """
    raws = [promise.RawPromise("预计1小时内送达", 1, "hour")]
    out = promise.evaluate(conn, "S00051", raws, as_of=datetime(2026, 5, 23, 10, 4, 48))
    p = out[0]
    assert p.ticket_no == "WL840101369"
    assert p.closed is True
    assert p.deadline_at == "2026-05-07 16:52:20"
    assert p.ticket_finished_at == "2026-05-07 21:33:03"
    assert p.overdue is True, "工单晚于 deadline 完结，必须判逾期"


def test_ticket_finished_before_deadline_is_not_overdue(conn):
    """反向钉子：同一张工单，deadline 落在完结时间之后就不该判逾期。"""
    raws = [promise.RawPromise("预计1小时内送达", 24, "hour")]
    out = promise.evaluate(conn, "S00051", raws, as_of=datetime(2026, 5, 23, 10, 4, 48))
    p = out[0]
    assert p.closed is True
    assert p.deadline_at == "2026-05-08 15:52:20"
    assert p.overdue is False


def test_is_overdue_at_is_pure(conn):
    raws = [promise.RawPromise("若3个工作日内仍未到账，我们走线下打款直接补给您",
                               3, "business_day")]
    p = promise.evaluate(conn, "S00005", raws, as_of=datetime(2026, 5, 6))[0]
    assert promise.is_overdue_at(p, datetime(2026, 5, 6)) is False
    assert promise.is_overdue_at(p, datetime(2026, 5, 9)) is True
