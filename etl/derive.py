"""派生表构建。全部幂等：先 DELETE 再全量重建。"""
import json
import sqlite3
from collections import Counter, defaultdict

from etl.schema import CLOSED_STATUS, TICKET_TABLES


def build_scene_map(conn: sqlite3.Connection) -> int:
    """scene_minor -> scene_major 的确定性映射表（spec §2.6 / §4.3）。"""
    pairs = conn.execute(
        "SELECT DISTINCT scene_minor, scene_major FROM chat "
        "WHERE scene_minor IS NOT NULL"
    ).fetchall()

    seen: dict[str, str] = {}
    for r in pairs:
        minor, major = r["scene_minor"], r["scene_major"]
        if minor in seen and seen[minor] != major:
            raise ValueError(
                f"scene_minor「{minor}」映射到多个 major: {seen[minor]} / {major}；"
                "spec §4.3 的 minor-only 策略前提被破坏"
            )
        seen[minor] = major

    conn.execute("DELETE FROM scene_map")
    conn.executemany(
        "INSERT INTO scene_map (scene_minor, scene_major) VALUES (?, ?)",
        sorted(seen.items()),
    )
    conn.commit()
    return len(seen)


def _ticket_stats(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """买家 -> (工单总数, 未完结工单数)。跨 5 张工单表聚合。"""
    total: Counter = Counter()
    open_: Counter = Counter()
    for table in TICKET_TABLES:
        for r in conn.execute(f"SELECT buyer, status FROM {table}"):
            total[r["buyer"]] += 1
            if r["status"] != CLOSED_STATUS:
                open_[r["buyer"]] += 1
    return {b: (total[b], open_[b]) for b in total}


def build_buyer_profile(conn: sqlite3.Connection) -> int:
    sessions: dict[str, set] = defaultdict(set)
    scenes: dict[str, Counter] = defaultdict(Counter)
    first: dict[str, str] = {}
    last: dict[str, str] = {}

    for r in conn.execute(
        "SELECT buyer, session_id, scene_major, sent_at FROM chat ORDER BY sent_at"
    ):
        b = r["buyer"]
        sessions[b].add(r["session_id"])
        if r["scene_major"]:
            scenes[b][r["scene_major"]] += 1
        first.setdefault(b, r["sent_at"])
        last[b] = r["sent_at"]

    orders: dict[str, list[float]] = defaultdict(list)
    for r in conn.execute("SELECT buyer, paid_amount FROM orders"):
        orders[r["buyer"]].append(float(r["paid_amount"] or 0))

    tickets = _ticket_stats(conn)

    rows = []
    for b in sessions:
        t_total, t_open = tickets.get(b, (0, 0))
        rows.append((
            b, len(sessions[b]), len(orders.get(b, [])), sum(orders.get(b, [])),
            t_total, t_open,
            json.dumps(dict(scenes[b]), ensure_ascii=False),
            first.get(b), last.get(b), None,
        ))

    conn.execute("DELETE FROM buyer_profile")
    conn.executemany(
        "INSERT INTO buyer_profile (buyer, session_count, order_count, total_paid,"
        " ticket_count, open_ticket_count, scene_dist, first_contact_at,"
        " last_contact_at, risk_level) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def build_all(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "scene_map": build_scene_map(conn),
        "buyer_profile": build_buyer_profile(conn),
    }
