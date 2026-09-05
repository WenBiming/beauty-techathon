import datetime

import pytest

from core import clock
from etl import db


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    yield c
    c.close()


def test_global_now_is_dataset_end(conn):
    assert clock.GLOBAL_NOW == "2026-05-23 10:04:48"
    assert clock.reference_now(conn) == datetime.datetime(2026, 5, 23, 10, 4, 48)


def test_session_clock_is_last_message_time(conn):
    """S00099 是演示主样本，客服接入那一刻的时间。"""
    assert clock.reference_now(conn, "S00099") == datetime.datetime(2026, 5, 9, 11, 34, 5)


def test_session_clock_differs_from_global(conn):
    assert clock.reference_now(conn, "S00005") < clock.reference_now(conn)


def test_unknown_session_falls_back_to_global(conn):
    assert clock.reference_now(conn, "S99999") == clock.reference_now(conn)


def test_add_business_days_within_week():
    # 2026-05-05 是周二，+3 个工作日 = 05-08 周五
    tue = datetime.datetime(2026, 5, 5, 12, 0)
    assert clock.add_business_days(tue, 3) == datetime.datetime(2026, 5, 8, 12, 0)


def test_add_business_days_skips_weekend():
    # 2026-05-07 是周四，+2 个工作日跨过周末 = 05-11 周一
    thu = datetime.datetime(2026, 5, 7, 9, 0)
    assert clock.add_business_days(thu, 2) == datetime.datetime(2026, 5, 11, 9, 0)


def test_add_zero_business_days_is_identity():
    d = datetime.datetime(2026, 5, 5, 9, 0)
    assert clock.add_business_days(d, 0) == d
