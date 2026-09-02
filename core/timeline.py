"""统一事件流：把聊天 / 订单 / 工单按时间归并成一条跨会话服务轨迹。

这是「多源跨维度融合」的技术落点，也是插件第一屏与看板下钻的共同数据源
（spec §3.4）。只读，不写库。
"""
import sqlite3
from dataclasses import dataclass

from etl.schema import TICKET_TABLES

CLOSED = "已完结"

# 工单表 -> 展示用中文名
TICKET_LABEL = {
    "ticket_reissue": "补发换货工单",
    "ticket_payout": "线下打款工单",
    "ticket_logistics": "物流工单",
    "ticket_adverse": "不良反应工单",
    "ticket_return": "售后退货工单",
}


@dataclass(frozen=True)
class TimelineEvent:
    ts: str
    kind: str                 # "chat" | "order" | "ticket" | "promise"
    session_id: str | None
    buyer: str
    title: str
    detail: str
    ref_id: str | None
    is_open: bool = False


def _chat_events(rows) -> list[TimelineEvent]:
    return [
        TimelineEvent(
            ts=r["sent_at"], kind="chat", session_id=r["session_id"],
            buyer=r["buyer"], title=r["role"],
            detail=r["message_text"] or "", ref_id=r["message_id"],
        )
        for r in rows
    ]


def _order_events(rows) -> list[TimelineEvent]:
    out = []
    for r in rows:
        for col, label in (("created_at", "下单"), ("paid_at", "付款"),
                           ("shipped_at", "发货")):
            if not r[col]:
                continue
            out.append(TimelineEvent(
                ts=r[col], kind="order", session_id=r["session_id"],
                buyer=r["buyer"], title=f"订单{label}",
                detail=f"{r['item_name']} ×{r['qty']} 实付{r['paid_amount']}元"
                       f"（{r['order_status']}）",
                ref_id=r["order_no"],
            ))
    return out


def _ticket_events(conn: sqlite3.Connection, where: str, param: str
                   ) -> list[TimelineEvent]:
    out = []
    for table in TICKET_TABLES:
        label = TICKET_LABEL[table]
        for r in conn.execute(
            f"SELECT * FROM {table} WHERE {where} = ?", (param,)
        ):
            is_open = r["status"] != CLOSED
            keys = r.keys()
            reason = (r["reason"] if "reason" in keys
                      else r["symptom"] if "symptom" in keys else "")
            out.append(TimelineEvent(
                ts=r["created_at"], kind="ticket", session_id=r["session_id"],
                buyer=r["buyer"], title=f"创建{label}",
                detail=f"{reason or ''}（{r['status']}）".strip("（）"),
                ref_id=r["ticket_no"], is_open=is_open,
            ))
            if r["finished_at"]:
                out.append(TimelineEvent(
                    ts=r["finished_at"], kind="ticket",
                    session_id=r["session_id"], buyer=r["buyer"],
                    title=f"{label}完结", detail=reason or "",
                    ref_id=r["ticket_no"], is_open=False,
                ))
    return out


def _promise_events(conn: sqlite3.Connection, where: str, param: str
                    ) -> list[TimelineEvent]:
    """承诺事件（spec §3.4）。M1 中 promise 表为空，M2 填充后自动生效。"""
    return [
        TimelineEvent(
            ts=r["made_at"], kind="promise", session_id=r["session_id"],
            buyer=r["buyer"],
            title="客服承诺" + ("（已逾期）" if r["overdue"] else ""),
            detail=f"{r['promise_text']}"
                   + (f" · 期限 {r['deadline_at']}" if r["deadline_at"] else ""),
            ref_id=r["message_id"], is_open=not r["closed"],
        )
        for r in conn.execute(f"SELECT * FROM promise WHERE {where} = ?", (param,))
    ]


def buyer_timeline(conn: sqlite3.Connection, buyer: str) -> list[TimelineEvent]:
    """该买家的跨会话全轨迹，按时间升序。"""
    evs = _chat_events(
        conn.execute("SELECT * FROM chat WHERE buyer = ?", (buyer,)))
    evs += _order_events(
        conn.execute("SELECT * FROM orders WHERE buyer = ?", (buyer,)))
    evs += _ticket_events(conn, "buyer", buyer)
    evs += _promise_events(conn, "buyer", buyer)
    return sorted(evs, key=lambda e: e.ts)


def session_timeline(conn: sqlite3.Connection, session_id: str
                     ) -> list[TimelineEvent]:
    """单个会话的事件流，按时间升序。"""
    evs = _chat_events(
        conn.execute("SELECT * FROM chat WHERE session_id = ?", (session_id,)))
    evs += _order_events(
        conn.execute("SELECT * FROM orders WHERE session_id = ?", (session_id,)))
    evs += _ticket_events(conn, "session_id", session_id)
    evs += _promise_events(conn, "session_id", session_id)
    return sorted(evs, key=lambda e: e.ts)
