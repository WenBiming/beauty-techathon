import json

import pytest

from etl import db, derive, loader


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.create_tables(c)
    loader.load_raw_tables(c)
    yield c
    c.close()


def test_scene_map_is_41_rows(conn):
    assert derive.build_scene_map(conn) == 41


def test_scene_map_is_one_to_one(conn):
    """41 个 scene_minor 严格映射唯一 scene_major（spec §2.6 的核心发现）。"""
    derive.build_scene_map(conn)
    rows = conn.execute("SELECT scene_minor, scene_major FROM scene_map").fetchall()
    assert len(rows) == 41
    assert len({r["scene_major"] for r in rows}) == 10
    assert dict(conn.execute(
        "SELECT scene_minor, scene_major FROM scene_map"
    ).fetchall())["退款迟迟不到账"] == "订单服务"


def test_buyer_profile_is_112_rows(conn):
    assert derive.build_buyer_profile(conn) == 112


def test_buyer_profile_of_demo_sample(conn):
    """演示金样本 魏h**：3 会话 / 3 订单 / 实付 556 元 / 1 张未完结工单。"""
    derive.build_buyer_profile(conn)
    r = conn.execute(
        "SELECT * FROM buyer_profile WHERE buyer = ?", ("魏h**",)).fetchone()
    assert r["session_count"] == 3
    assert r["order_count"] == 3
    assert r["total_paid"] == pytest.approx(556.0)
    assert r["ticket_count"] == 1
    assert r["open_ticket_count"] == 1
    assert r["first_contact_at"] == "2026-05-05 12:05:11"
    assert r["last_contact_at"] == "2026-05-09 11:34:05"
    assert set(json.loads(r["scene_dist"])) == {"订单服务", "售后退货", "物流服务"}


def test_repeat_contact_buyers_count(conn):
    """21% 重复进线率：22 个买家 2 次 + 2 个买家 3 次 = 24 个。"""
    derive.build_buyer_profile(conn)
    n = conn.execute(
        "SELECT COUNT(*) c FROM buyer_profile WHERE session_count > 1").fetchone()["c"]
    assert n == 24


def test_build_all_is_idempotent(conn):
    first = derive.build_all(conn)
    second = derive.build_all(conn)
    assert first == second == {"scene_map": 41, "buyer_profile": 112}
