"""承诺 deadline 解析与逾期判定。纯逻辑，不调模型。

L1 只负责从客服话术里抽出 (原文, 数量, 单位)，绝对时间换算与闭环判定放在
这里——可测、可解释、零成本。

overdue 依赖「何时看」：承诺在会话中做出、deadline 通常在会话结束之后，
用该会话自己的时钟判断永远不逾期。所以 evaluate 要传 as_of：存库用全局
时钟（看板视角），插件展示用 is_overdue_at 按情景时钟现算。

软承诺（amount 为 None，如「马上帮您查」）只记录不预警，否则误报会淹没
真信号。
"""
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from core.clock import add_business_days
from etl.schema import CLOSED_STATUS, TICKET_TABLES

MATCH_PREFIX = 12          # locate_message 用前 N 字做子串匹配


@dataclass(frozen=True)
class RawPromise:
    text: str
    amount: int | None
    unit: str | None            # "hour" | "day" | "business_day" | None


@dataclass(frozen=True)
class ResolvedPromise:
    message_id: str
    session_id: str
    buyer: str
    promise_text: str
    promise_type: str           # "hard" | "soft"
    made_at: str
    deadline_at: str | None
    ticket_no: str | None
    closed: bool
    overdue: bool
    # 工单完结时间。closed 只说「完结了」，不说「什么时候完结」——
    # 晚于 deadline 才完结的承诺是真逾期（I6）。
    ticket_finished_at: str | None = None


def resolve_deadline(made_at: datetime, amount: int | None,
                     unit: str | None) -> datetime | None:
    if amount is None or unit is None:
        return None
    if unit == "hour":
        return made_at + timedelta(hours=amount)
    if unit == "day":
        return made_at + timedelta(days=amount)
    if unit == "business_day":
        return add_business_days(made_at, amount)
    raise ValueError(f"未知时间单位 {unit!r}，应为 hour/day/business_day/None")


def locate_message(conn: sqlite3.Connection, session_id: str,
                   text: str) -> sqlite3.Row | None:
    """找出承诺出自哪一条客服消息。用前 MATCH_PREFIX 字做子串匹配。"""
    needle = (text or "")[:MATCH_PREFIX]
    if not needle:
        return None
    for r in conn.execute(
        "SELECT * FROM chat WHERE session_id = ? AND role = '客服' ORDER BY sent_at",
        (session_id,),
    ):
        if needle in (r["message_text"] or ""):
            return r
    return None


def session_ticket(conn: sqlite3.Connection,
                    session_id: str) -> tuple[str | None, bool, str | None]:
    """该会话关联的工单号 / 是否已完结 / 完结时间。一个会话至多一张工单（spec §2.1）。"""
    for table in TICKET_TABLES:
        r = conn.execute(
            f"SELECT ticket_no, status, finished_at FROM {table} WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if r is not None:
            return (r["ticket_no"], r["status"] == CLOSED_STATUS,
                    r["finished_at"] or None)
    return None, False, None


def is_overdue_at(p: ResolvedPromise, as_of: datetime) -> bool:
    """按给定时刻判断是否逾期。软承诺永不逾期。

    「已闭环」不等于「按时兑现」（I6）：工单晚于 deadline 才完结的承诺，
    买家等待的那段时间是真实的服务失约——正是 spec 反复强调的「隐性服务
    风险」。只看 status 会在本作品的招牌能力上制造假阴性。
    """
    if p.promise_type == "soft" or p.deadline_at is None:
        return False
    deadline = datetime.fromisoformat(p.deadline_at)
    if p.closed and p.ticket_finished_at:
        # 工单已完结：看它是不是在 deadline 之前完结的
        return datetime.fromisoformat(p.ticket_finished_at) > deadline
    if p.closed:
        return False          # 已完结但无完结时间，保守判不逾期
    return deadline < as_of


def evaluate(conn: sqlite3.Connection, session_id: str,
             raws: list[RawPromise], as_of: datetime) -> list[ResolvedPromise]:
    fallback = conn.execute(
        "SELECT * FROM chat WHERE session_id = ? AND role = '客服'"
        " ORDER BY sent_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    ticket_no, ticket_closed, ticket_finished_at = session_ticket(conn, session_id)

    out: list[ResolvedPromise] = []
    for raw in raws:
        row = locate_message(conn, session_id, raw.text) or fallback
        if row is None:
            continue
        made = datetime.fromisoformat(row["sent_at"])
        deadline = resolve_deadline(made, raw.amount, raw.unit)
        p = ResolvedPromise(
            message_id=row["message_id"],
            session_id=session_id,
            buyer=row["buyer"],
            promise_text=raw.text,
            promise_type="hard" if deadline is not None else "soft",
            made_at=row["sent_at"],
            deadline_at=deadline.isoformat(sep=" ") if deadline else None,
            ticket_no=ticket_no,
            closed=ticket_closed,
            overdue=False,
            ticket_finished_at=ticket_finished_at,
        )
        out.append(replace(p, overdue=is_overdue_at(p, as_of)))
    return out
