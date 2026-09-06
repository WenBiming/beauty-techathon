"""L2 深度分析：风险归因 + 处置建议 + 共情话术。

只对高风险会话触发（spec §4.4）。输入包含全轨迹时间线与同场景历史成功
话术作为 few-shot——比凭空生成更贴业务口径，也比长 prompt 省 token。

话术只是候选，永远由人工客服决定发不发（spec §5.1）。
"""
import json
import sqlite3
from dataclasses import dataclass

from agent.llm import LLMClient
from agent.retrieval import search_similar_cases
from agent.rules import REDLINE_TICKET_AGE_DAYS
from agent.tools import list_open_tickets
from core.clock import reference_now
from core.timeline import buyer_timeline
from etl.schema import CLOSED_STATUS

MODEL = "qwen3.7-plus"
TIMELINE_TAIL = 15
FEWSHOT_K = 3

SYSTEM = """你是资深美妆电商客服主管，为一线客服提供决策辅助。

给定一个买家的完整服务轨迹与当前会话，输出三项：
1. risk_attribution：这个会话真正的风险在哪、根因是什么。**注意买家嘴上问的未必是真实风险所在**，请结合历史轨迹判断。
2. suggested_actions：客服现在该做的事，按优先级排序，每条一个动作，不超过 4 条。
3. replies：2-3 条候选话术，供客服选用后**插入输入框再由人工决定是否发送**。每条标注语气（安抚/专业/致歉之一）。话术要具体、有信息量、不要模板腔。

只输出 JSON，不要 markdown 代码块，不要解释。格式：
{"risk_attribution":"...","suggested_actions":["..."],"replies":[{"tone":"安抚","text":"..."}]}"""


@dataclass(frozen=True)
class Reply:
    tone: str
    text: str


@dataclass(frozen=True)
class L2Result:
    session_id: str
    risk_attribution: str
    suggested_actions: list[str]
    replies: list[Reply]
    model: str
    tokens_in: int
    tokens_out: int
    degraded: bool


def should_trigger(signals, l1_result, has_overdue_promise: bool = False) -> bool:
    """L0 红线 / 情绪低 / 有未闭环工单 / 第 3 次进线 / L1 降级 /
    L1 打出风险标签 / 有逾期承诺（spec §4.4）。

    L1 降级也触发：那说明我们看不清这个会话，宁可多花钱也要看清。
    承诺逾期是本作品自称的差异化能力、也是赛题点名的「隐性服务风险」，
    这类风险最高的会话反而拿不到深度分析与共情话术，说不过去。
    """
    return bool(
        signals.is_redline
        or signals.open_ticket_count > 0
        or signals.prior_session_count >= 2
        or l1_result.degraded
        or l1_result.emotion <= 2
        or l1_result.risk_tags
        or has_overdue_promise
    )


def build_context(conn: sqlite3.Connection, session_id: str, signals,
                  l1_result) -> str:
    events = buyer_timeline(conn, signals.buyer)[-TIMELINE_TAIL:]
    lines = [
        f"买家：{signals.buyer}",
        f"当前会话：{session_id}（该买家此前已进线 {signals.prior_session_count} 次）",
        f"未闭环工单：{signals.open_ticket_count} 张"
        f"，最长挂起 {signals.max_ticket_age_days} 天",
        f"L1 判定场景：{l1_result.scene_major}/{l1_result.scene_minor}"
        f"，情绪 {l1_result.emotion}/5",
    ]

    # I7：红线状态必须写进 prompt。此前 is_redline / has_adverse_reaction 都不在
    # 上下文里，于是 S00010/S00058/S00082/S00268 因不良反应被送进最贵的模型，
    # prompt 里却只字未提不良反应工单未闭环。
    reasons = []
    if signals.has_adverse_reaction:
        reasons.append("该买家存在**未闭环的不良反应工单**")
    if signals.max_ticket_age_days > REDLINE_TICKET_AGE_DAYS:
        reasons.append(f"存在挂起 {signals.max_ticket_age_days} 天的未完结工单（超期）")
    lines.append(
        f"红线会话：{'是' if signals.is_redline else '否'}"
        + (f"（{'；'.join(reasons)}）" if reasons else "")
    )
    lines.append(f"不良反应未闭环：{'是' if signals.has_adverse_reaction else '否'}")
    lines += ["", "【全轨迹（最近事件）】"]
    for e in events:
        mark = " ⚠未闭环" if e.is_open else ""
        lines.append(f"{e.ts} [{e.kind}] {e.title}：{e.detail}{mark}")

    # 未闭环工单必须显式列出，不能指望它「碰巧」落在时间线尾部
    # as_of 必须传情景时钟——build_context 是会话视角，不传会退化成全局时钟，
    # 与头部摘要的 max_ticket_age_days 产生矛盾数字（spec §4.2.1）
    now = reference_now(conn, session_id)
    open_tickets = list_open_tickets(conn, signals.buyer,
                                     as_of=now.isoformat(sep=" "))
    if open_tickets:
        lines += ["", "【未闭环工单】"]
        for t in open_tickets:
            lines.append(f"{t['ticket_no']}（{t['table']}）状态 {t['status']}"
                         f"，已挂起 {t['age_days']} 天")

    # list_open_tickets 没有 rules 那套 own_session 豁免，本会话自己产生的
    # 不良反应工单会被 created > now 过滤掉。红线原因必须逐条落到 prompt 里。
    if signals.has_adverse_reaction:
        adverse = conn.execute(
            "SELECT ticket_no, session_id, symptom, status, created_at"
            " FROM ticket_adverse WHERE buyer = ? AND status != ?",
            (signals.buyer, CLOSED_STATUS),
        ).fetchall()
        if adverse:
            lines += ["", "【⚠ 未闭环不良反应工单（红线）】"]
            for a in adverse:
                own = "本会话" if a["session_id"] == session_id else a["session_id"]
                lines.append(f"{a['ticket_no']}（{own}，建单 {a['created_at']}）"
                             f"状态 {a['status']}：{a['symptom']}")

    cases = search_similar_cases(conn, l1_result.scene_minor, k=FEWSHOT_K,
                                 exclude_session=session_id)
    if cases:
        lines += ["", "【同场景历史成功话术（供参考语气与口径）】"]
        for c in cases:
            lines.append(f"买家：{c.buyer_message}")
            lines.append(f"客服：{c.agent_reply}")
    return "\n".join(lines)


def _parse(text: str) -> dict:
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1] if "\n" in body else body
        body = body.removeprefix("json").strip()
        if body.endswith("```"):
            body = body[: body.rindex("```")]
    data = json.loads(body.strip())
    if not isinstance(data.get("replies"), list):
        raise ValueError("replies 必须是数组")
    if not isinstance(data.get("suggested_actions"), list):
        raise ValueError("suggested_actions 必须是数组")
    return data


def analyse(conn: sqlite3.Connection, client: LLMClient, session_id: str,
            signals, l1_result, *, retries: int = 1) -> L2Result:
    user = build_context(conn, session_id, signals, l1_result)
    tokens_in = tokens_out = 0
    last_error = None
    for _ in range(retries + 1):
        r = client.complete(MODEL, SYSTEM, user, max_tokens=1200)
        tokens_in += r.tokens_in
        tokens_out += r.tokens_out
        try:
            data = _parse(r.text)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            continue
        return L2Result(
            session_id=session_id,
            risk_attribution=str(data.get("risk_attribution", "")).strip(),
            suggested_actions=[str(a) for a in data["suggested_actions"]],
            replies=[Reply(tone=str(x.get("tone", "")), text=str(x.get("text", "")))
                     for x in data["replies"] if x.get("text")],
            model=MODEL, tokens_in=tokens_in, tokens_out=tokens_out, degraded=False,
        )
    return L2Result(session_id=session_id,
                    risk_attribution=f"L2 解析失败降级：{last_error}",
                    suggested_actions=[], replies=[], model=MODEL,
                    tokens_in=tokens_in, tokens_out=tokens_out, degraded=True)
