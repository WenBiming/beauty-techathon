"""L0 规则层：零 token 的确定性信号。

这些信号有唯一正确答案，SQL 一定对而模型可能错，所以不交给大模型（spec §4.2）。
时间基准用情景时钟——客服接入那一刻真实看到的状态。
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from core.clock import reference_now
from etl.schema import CLOSED_STATUS, TICKET_TABLES

REDLINE_TICKET_AGE_DAYS = 3


@dataclass(frozen=True)
class L0Signals:
    session_id: str
    buyer: str
    prior_session_count: int
    hours_since_prior: float | None
    open_ticket_count: int
    max_ticket_age_days: int
    has_adverse_reaction: bool
    refund_ticket_count: int
    max_response_gap_sec: int
    turn_count: int
    is_redline: bool


def _session_rows(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT sent_at, role, buyer FROM chat WHERE session_id = ? ORDER BY sent_at",
        (session_id,),
    ).fetchall()


def compute(conn: sqlite3.Connection, session_id: str) -> L0Signals:
    rows = _session_rows(conn, session_id)
    if not rows:
        raise KeyError(f"未知会话 {session_id}")
    buyer = rows[0]["buyer"]
    now = reference_now(conn, session_id)
    start = datetime.fromisoformat(rows[0]["sent_at"])

    prior = conn.execute(
        "SELECT DISTINCT session_id, MAX(sent_at) AS t FROM chat"
        " WHERE buyer = ? AND sent_at < ? GROUP BY session_id ORDER BY t DESC",
        (buyer, rows[0]["sent_at"]),
    ).fetchall()
    hours_since_prior = None
    if prior:
        hours_since_prior = (
            start - datetime.fromisoformat(prior[0]["t"])
        ).total_seconds() / 3600

    open_count = 0
    max_age = 0
    adverse = False
    refunds = 0
    for table in TICKET_TABLES:
        for r in conn.execute(f"SELECT * FROM {table} WHERE buyer = ?", (buyer,)):
            created = datetime.fromisoformat(r["created_at"])
            own_session = r["session_id"] == session_id
            # 全库 80/80 张工单都建单于其会话结束之后。本会话自己的工单是客服
            # 正在处理的事，他当然知道；其它会话的工单只有建单早于接入时点才看得见。
            if not own_session and created > now:
                continue
            if table in ("ticket_payout", "ticket_return"):
                refunds += 1
            # 红线：本会话或历史遗留的未闭环不良反应工单
            if table == "ticket_adverse" and r["status"] != CLOSED_STATUS:
                adverse = True
            # 「信息孤岛」信号：只数此前遗留的未闭环工单，不含本会话自己产生的
            if r["status"] != CLOSED_STATUS and not own_session:
                open_count += 1
                max_age = max(max_age, (now - created).days)

    gaps = []
    for a, b in zip(rows, rows[1:]):
        if a["role"] == "买家" and b["role"] == "客服":
            gaps.append(
                int(
                    (
                        datetime.fromisoformat(b["sent_at"])
                        - datetime.fromisoformat(a["sent_at"])
                    ).total_seconds()
                )
            )

    redline = adverse or max_age > REDLINE_TICKET_AGE_DAYS

    return L0Signals(
        session_id=session_id,
        buyer=buyer,
        prior_session_count=len(prior),
        hours_since_prior=hours_since_prior,
        open_ticket_count=open_count,
        max_ticket_age_days=max_age,
        has_adverse_reaction=adverse,
        refund_ticket_count=refunds,
        max_response_gap_sec=max(gaps) if gaps else 0,
        turn_count=len(rows),
        is_redline=redline,
    )


def compute_all(conn: sqlite3.Connection) -> dict[str, L0Signals]:
    ids = [
        r["session_id"]
        for r in conn.execute("SELECT DISTINCT session_id FROM chat ORDER BY session_id")
    ]
    return {sid: compute(conn, sid) for sid in ids}
