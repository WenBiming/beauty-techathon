"""数据质量报告。ETL 每次运行都产出，用于及早发现数据假设被打破。"""
import sqlite3
from dataclasses import dataclass, field

from etl.schema import TICKET_TABLES

MOCK_DISCLAIMER = (
    "⚠ 本数据集全部内容为官方提供的 AI 生成虚构 MOCK 数据，"
    "与任何真实企业、品牌、个人或交易无关，不得用于生产用途。"
)


@dataclass
class QualityReport:
    session_count: int
    buyer_count: int
    message_count: int
    order_count: int
    ticket_count: int
    open_ticket_count: int
    orphan_order_sessions: list[str]
    orphan_ticket_sessions: list[str]
    consult_only_sessions: list[str]
    collision_suspects: list[dict] = field(default_factory=list)


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    return conn.execute(sql).fetchone()[0]


def check(conn: sqlite3.Connection) -> QualityReport:
    chat_sessions = {
        r["session_id"] for r in conn.execute("SELECT DISTINCT session_id FROM chat")
    }
    order_sessions = {
        r["session_id"] for r in conn.execute("SELECT DISTINCT session_id FROM orders")
    }
    ticket_sessions: set[str] = set()
    ticket_total = 0
    open_total = 0
    for t in TICKET_TABLES:
        for r in conn.execute(f"SELECT session_id, status FROM {t}"):
            ticket_sessions.add(r["session_id"])
            ticket_total += 1
            if r["status"] != "已完结":
                open_total += 1

    # 昵称碰撞嫌疑：同一昵称的订单收货省份多于 1 个
    suspects = []
    rows = conn.execute(
        "SELECT buyer, GROUP_CONCAT(DISTINCT province) provs FROM orders"
        " WHERE province IS NOT NULL GROUP BY buyer"
    ).fetchall()
    profile = dict(conn.execute(
        "SELECT buyer, session_count FROM buyer_profile").fetchall())
    for r in rows:
        provs = sorted(set((r["provs"] or "").split(",")))
        if len(provs) > 1:
            suspects.append({
                "buyer": r["buyer"],
                "provinces": provs,
                "session_count": profile.get(r["buyer"], 0),
            })

    return QualityReport(
        session_count=len(chat_sessions),
        buyer_count=_scalar(conn, "SELECT COUNT(DISTINCT buyer) FROM chat"),
        message_count=_scalar(conn, "SELECT COUNT(*) FROM chat"),
        order_count=_scalar(conn, "SELECT COUNT(*) FROM orders"),
        ticket_count=ticket_total,
        open_ticket_count=open_total,
        orphan_order_sessions=sorted(order_sessions - chat_sessions),
        orphan_ticket_sessions=sorted(ticket_sessions - chat_sessions),
        consult_only_sessions=sorted(
            chat_sessions - order_sessions - ticket_sessions),
        collision_suspects=suspects,
    )


def format_report(report: QualityReport) -> str:
    lines = [
        MOCK_DISCLAIMER,
        "",
        "=== 数据质量报告 ===",
        f"会话 {report.session_count} | 买家 {report.buyer_count} "
        f"| 消息 {report.message_count} | 订单 {report.order_count}",
        f"工单 {report.ticket_count}（未完结 {report.open_ticket_count}）",
        f"孤儿订单会话: {len(report.orphan_order_sessions)}",
        f"孤儿工单会话: {len(report.orphan_ticket_sessions)}",
        f"纯咨询会话（无单无工单，走插件降级模式）: "
        f"{len(report.consult_only_sessions)}",
        f"昵称碰撞嫌疑（同昵称跨省收货，需人工核对）: "
        f"{len(report.collision_suspects)}",
    ]
    for s in report.collision_suspects:
        lines.append(
            f"   - {s['buyer']}: {'/'.join(s['provinces'])} "
            f"（{s['session_count']} 个会话）")
    return "\n".join(lines)
