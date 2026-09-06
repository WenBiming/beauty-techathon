"""六类风险事件生成（spec §6.1）。

赛题点名的四类（舆情投诉/重复进线/情绪升级/重复退款）全覆盖；承诺逾期与
不良反应未闭环是从数据里挖出来的增量。

risk_event 的唯一索引是 (risk_type, session_id, detected_by)，所以同一会话
同一类风险只留一条，重跑幂等。

时间基准：risk_event 是**看板与插件共用的同一份**，看板是主管视角，所以
表内所有时间判定一律用**全局时钟**（spec §4.2.1）。不要消费 rules.compute
的 max_ticket_age_days——那是情景时钟下的会话视角信号，给插件用的；混进来
会让同一张表里承诺逾期用全局时钟、工单龄期用情景时钟，自相矛盾。
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from agent.rules import REDLINE_TICKET_AGE_DAYS
from core.clock import reference_now
from etl.schema import CLOSED_STATUS, TICKET_TABLES

# 超期天数阈值只有一份定义，在 rules.py（I10）。
TICKET_AGE_ALERT_DAYS = REDLINE_TICKET_AGE_DAYS
REFUND_ALERT_COUNT = 2

RISK_TYPES = [
    "不良反应未闭环", "承诺逾期", "重复进线",
    "工单积压超期", "情绪升级", "重复退款风控",
]
# 生产代码引用这些具名常量，测试才能真正钉住 detect() 的输出（I11）。
(RISK_ADVERSE, RISK_PROMISE_OVERDUE, RISK_REPEAT_CONTACT,
 RISK_TICKET_AGE, RISK_EMOTION, RISK_REFUND) = RISK_TYPES


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
    global_now = reference_now(conn)          # 看板视角：全局时钟

    # 1. 不良反应未闭环（红线，L0 直读工单表）
    for r in conn.execute(
        "SELECT ticket_no, session_id, buyer, symptom, status FROM ticket_adverse"
        " WHERE status != ?", (CLOSED_STATUS,)
    ):
        out.append(RiskEvent(
            risk_type=RISK_ADVERSE, level="红", session_id=r["session_id"],
            buyer=r["buyer"], ticket_no=r["ticket_no"], detected_by="L0",
            detail=f"{r['symptom']}（工单状态：{r['status']}）",
        ))

    # 2. 承诺逾期（L1 抽取 + L0 判定）
    for p in promises:
        if p.overdue:
            out.append(RiskEvent(
                risk_type=RISK_PROMISE_OVERDUE, level="橙", session_id=p.session_id,
                buyer=p.buyer, ticket_no=p.ticket_no, detected_by="L0",
                detail=f"承诺「{p.promise_text}」应于 {p.deadline_at} 前兑现，未闭环",
            ))

    # 4. 工单积压超期（L0 直读工单表，全局时钟）
    #    与第 1 类同样直读，不走 signals——signals 是情景时钟下的会话视角。
    for table in TICKET_TABLES:
        for r in conn.execute(
            f"SELECT ticket_no, session_id, buyer, status, created_at FROM {table}"
            " WHERE status != ?", (CLOSED_STATUS,)
        ):
            created = datetime.fromisoformat(r["created_at"])
            if created > global_now:
                continue
            age = (global_now - created).days
            if age <= TICKET_AGE_ALERT_DAYS:
                continue
            out.append(RiskEvent(
                risk_type=RISK_TICKET_AGE, level="橙", session_id=r["session_id"],
                buyer=r["buyer"], ticket_no=r["ticket_no"], detected_by="L0",
                detail=f"工单 {r['ticket_no']} 已挂起 {age} 天仍为「{r['status']}」",
            ))

    for sid, s in signals.items():
        # 3. 重复进线
        if s.prior_session_count >= 1:
            out.append(RiskEvent(
                risk_type=RISK_REPEAT_CONTACT,
                level="红" if s.prior_session_count >= 2 else "橙",
                session_id=sid, buyer=s.buyer, ticket_no=None, detected_by="L0",
                detail=f"该买家此前已进线 {s.prior_session_count} 次",
            ))
        # 6. 重复退款 / 风控异常
        if s.refund_ticket_count >= REFUND_ALERT_COUNT:
            out.append(RiskEvent(
                risk_type=RISK_REFUND, level="橙", session_id=sid, buyer=s.buyer,
                ticket_no=None, detected_by="L0",
                detail=f"该买家累计 {s.refund_ticket_count} 张退款/退货类工单",
            ))
        # 5. 情绪升级（L1）
        r1 = l1_results.get(sid)
        if r1 is not None and not r1.degraded and r1.emotion <= 2:
            out.append(RiskEvent(
                risk_type=RISK_EMOTION,
                level="红" if r1.emotion == 1 else "橙",
                session_id=sid, buyer=s.buyer, ticket_no=None, detected_by="L1",
                detail=f"情绪评分 {r1.emotion}/5：{r1.summary}",
            ))
    return out


def persist(conn: sqlite3.Connection, events: list[RiskEvent]) -> int:
    """幂等写入。**必须是 upsert 而不是 INSERT OR REPLACE**（C1）。

    REPLACE 的语义是「删冲突行再插入」，会把主管在看板上标的 status='已闭环'、
    handler 一并抹掉，created_at 重置、AUTOINCREMENT 的 id 换新。而 spec §4.8
    要求界面留「实时重算」按钮并在演示视频里现场点一次——REPLACE 之下按一次
    所有已闭环预警全部归零，§6.2 的状态机与闭环率直接失效。

    所以只更新会随重算变化的字段（level / ticket_no / detail / updated_at），
    status / handler / created_at / id 一律不动。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO risk_event"
        " (risk_type, level, session_id, buyer, ticket_no, detected_by,"
        "  status, handler, detail, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, '待处理', NULL, ?, ?, ?)"
        " ON CONFLICT(risk_type, session_id, detected_by) DO UPDATE SET"
        "     level = excluded.level,"
        "     ticket_no = excluded.ticket_no,"
        "     detail = excluded.detail,"
        "     updated_at = excluded.updated_at",
        [(e.risk_type, e.level, e.session_id, e.buyer, e.ticket_no,
          e.detected_by, e.detail, now, now) for e in events],
    )
    conn.commit()
    return len(events)
