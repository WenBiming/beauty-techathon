"""六类风险事件生成（spec §6.1）。

赛题点名的四类（舆情投诉/重复进线/情绪升级/重复退款）全覆盖；承诺逾期与
不良反应未闭环是从数据里挖出来的增量。

risk_event 的唯一索引是 (risk_type, session_id, detected_by)，所以同一会话
同一类风险只留一条，重跑幂等。
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from etl.schema import CLOSED_STATUS

TICKET_AGE_ALERT_DAYS = 3
REFUND_ALERT_COUNT = 2

RISK_TYPES = [
    "不良反应未闭环", "承诺逾期", "重复进线",
    "工单积压超期", "情绪升级", "重复退款风控",
]


@dataclass(frozen=True)
class RiskEvent:
    risk_type: str
    level: str                  # "红" | "橙"
    session_id: str | None
    buyer: str
    ticket_no: str | None
    detected_by: str            # "L0" | "L1"
    detail: str


def detect(conn: sqlite3.Connection, signals: dict, l1_results: dict,
           promises: list) -> list[RiskEvent]:
    out: list[RiskEvent] = []

    # 1. 不良反应未闭环（红线，L0 直读工单表）
    for r in conn.execute(
        "SELECT ticket_no, session_id, buyer, symptom, status FROM ticket_adverse"
        " WHERE status != ?", (CLOSED_STATUS,)
    ):
        out.append(RiskEvent(
            risk_type="不良反应未闭环", level="红", session_id=r["session_id"],
            buyer=r["buyer"], ticket_no=r["ticket_no"], detected_by="L0",
            detail=f"{r['symptom']}（工单状态：{r['status']}）",
        ))

    # 2. 承诺逾期（L1 抽取 + L0 判定）
    for p in promises:
        if p.overdue:
            out.append(RiskEvent(
                risk_type="承诺逾期", level="橙", session_id=p.session_id,
                buyer=p.buyer, ticket_no=p.ticket_no, detected_by="L0",
                detail=f"承诺「{p.promise_text}」应于 {p.deadline_at} 前兑现，未闭环",
            ))

    for sid, s in signals.items():
        # 3. 重复进线
        if s.prior_session_count >= 1:
            out.append(RiskEvent(
                risk_type="重复进线",
                level="红" if s.prior_session_count >= 2 else "橙",
                session_id=sid, buyer=s.buyer, ticket_no=None, detected_by="L0",
                detail=f"该买家此前已进线 {s.prior_session_count} 次",
            ))
        # 4. 工单积压超期
        if s.max_ticket_age_days > TICKET_AGE_ALERT_DAYS:
            out.append(RiskEvent(
                risk_type="工单积压超期", level="橙", session_id=sid, buyer=s.buyer,
                ticket_no=None, detected_by="L0",
                detail=f"存在挂起 {s.max_ticket_age_days} 天的未完结工单",
            ))
        # 6. 重复退款 / 风控异常
        if s.refund_ticket_count >= REFUND_ALERT_COUNT:
            out.append(RiskEvent(
                risk_type="重复退款风控", level="橙", session_id=sid, buyer=s.buyer,
                ticket_no=None, detected_by="L0",
                detail=f"该买家累计 {s.refund_ticket_count} 张退款/退货类工单",
            ))
        # 5. 情绪升级（L1）
        r1 = l1_results.get(sid)
        if r1 is not None and not r1.degraded and r1.emotion <= 2:
            out.append(RiskEvent(
                risk_type="情绪升级",
                level="红" if r1.emotion == 1 else "橙",
                session_id=sid, buyer=s.buyer, ticket_no=None, detected_by="L1",
                detail=f"情绪评分 {r1.emotion}/5：{r1.summary}",
            ))
    return out


def persist(conn: sqlite3.Connection, events: list[RiskEvent]) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT OR REPLACE INTO risk_event"
        " (risk_type, level, session_id, buyer, ticket_no, detected_by,"
        "  status, handler, detail, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, '待处理', NULL, ?, ?, ?)",
        [(e.risk_type, e.level, e.session_id, e.buyer, e.ticket_no,
          e.detected_by, e.detail, now, now) for e in events],
    )
    conn.commit()
    return len(events)
