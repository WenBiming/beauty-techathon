"""L1 全量会话分析：意图 / 情绪 / 摘要 / 承诺抽取。

只预测 scene_minor（41 类），scene_major 由 scene_map 表反查——41→10 是严格
1:1 映射，让模型猜 major 是纯粹的错误来源（spec §4.3，实测把粗类准确率从
7/10 提到 9/10）。

四层可靠性：Schema 约束 -> 剥 markdown 围栏 -> 重试 1 次 -> 降级为规则结果。
批处理不能因为个别脏输出中断。
"""
import json
import sqlite3
from dataclasses import dataclass

from agent import prompts
from agent.llm import LLMClient
from agent.promise import RawPromise

MODEL = "qwen3.8-flash"
VALID_UNITS = {"hour", "day", "business_day"}


@dataclass(frozen=True)
class L1Result:
    session_id: str
    scene_minor: str
    scene_major: str
    confidence: float
    emotion: int
    summary: str
    risk_tags: list[str]
    high_risk: bool
    promises: list[RawPromise]
    model: str
    tokens_in: int
    tokens_out: int
    degraded: bool


def _scene_map(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["scene_minor"]: r["scene_major"]
            for r in conn.execute("SELECT scene_minor, scene_major FROM scene_map")}


def parse_payload(conn: sqlite3.Connection, text: str) -> dict:
    """剥围栏 + 解析 + 白名单校验。任何一步不过就抛 ValueError。"""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1] if "\n" in body else body
        body = body.removeprefix("json").strip()
        if body.endswith("```"):
            body = body[: body.rindex("```")]
    data = json.loads(body.strip())

    minor = data.get("scene_minor")
    if minor not in _scene_map(conn):
        raise ValueError(f"scene_minor 不在 41 类白名单内: {minor!r}")
    emotion = data.get("emotion")
    if not isinstance(emotion, int) or not 1 <= emotion <= 5:
        raise ValueError(f"emotion 必须是 1-5 的整数，得到 {emotion!r}")
    high_risk = data.get("high_risk")
    if not isinstance(high_risk, bool):
        raise ValueError(f"high_risk 必须是布尔值，得到 {high_risk!r}")
    if not isinstance(data.get("promises", []), list):
        raise ValueError("promises 必须是数组")
    return data


def _to_raw_promises(items) -> list[RawPromise]:
    out = []
    for it in items:
        unit = it.get("unit")
        amount = it.get("amount")
        if unit not in VALID_UNITS or not isinstance(amount, int):
            unit, amount = None, None          # 归一化为软承诺
        out.append(RawPromise(text=str(it.get("text", "")).strip(),
                              amount=amount, unit=unit))
    return [p for p in out if p.text]


def analyse(conn: sqlite3.Connection, client: LLMClient, session_id: str, *,
            retries: int = 1) -> L1Result:
    rows = conn.execute(
        "SELECT role, message_text FROM chat WHERE session_id = ? ORDER BY sent_at",
        (session_id,),
    ).fetchall()
    minors = sorted(_scene_map(conn))
    system = prompts.L1_SYSTEM(minors)
    user = prompts.render_dialogue(rows)

    tokens_in = tokens_out = 0
    last_error = None
    for _ in range(retries + 1):
        r = client.complete(MODEL, system, user)
        tokens_in += r.tokens_in
        tokens_out += r.tokens_out
        try:
            data = parse_payload(conn, r.text)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            continue
        return L1Result(
            session_id=session_id,
            scene_minor=data["scene_minor"],
            scene_major=_scene_map(conn)[data["scene_minor"]],
            confidence=float(data.get("confidence") or 0.0),
            emotion=int(data["emotion"]),
            summary=str(data.get("summary", "")).strip(),
            risk_tags=[str(t) for t in data.get("risk_tags", [])],
            high_risk=bool(data["high_risk"]),
            promises=_to_raw_promises(data.get("promises", [])),
            model=MODEL, tokens_in=tokens_in, tokens_out=tokens_out, degraded=False,
        )

    return L1Result(
        session_id=session_id, scene_minor="", scene_major="", confidence=0.0,
        emotion=3, summary=f"L1 解析失败降级：{last_error}", risk_tags=[],
        high_risk=False,
        promises=[], model=MODEL, tokens_in=tokens_in, tokens_out=tokens_out,
        degraded=True,
    )
