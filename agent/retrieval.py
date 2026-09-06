"""同场景历史成功话术检索（小 RAG）。

检索条件是精确的结构化字段（scene_minor），SQL 足够——138 个会话的规模上
向量库的成本远大于收益（spec §9）。返回「买家问 → 客服答」的相邻配对，
供 L2 作为 few-shot 使用。
"""
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SimilarCase:
    session_id: str
    scene_minor: str
    buyer_message: str
    agent_reply: str


def search_similar_cases(conn: sqlite3.Connection, scene_minor: str, k: int = 3,
                         exclude_session: str | None = None) -> list[SimilarCase]:
    sessions = [
        r["session_id"]
        for r in conn.execute(
            "SELECT DISTINCT session_id FROM chat WHERE scene_minor = ?"
            " ORDER BY session_id",
            (scene_minor,),
        )
        if r["session_id"] != exclude_session
    ]

    out: list[SimilarCase] = []
    for sid in sessions:
        if len(out) >= k:
            break
        rows = conn.execute(
            "SELECT role, message_text FROM chat WHERE session_id = ? ORDER BY sent_at",
            (sid,),
        ).fetchall()

        # 收集所有相邻的「买家→客服」配对
        candidates = []
        for a, b in zip(rows, rows[1:]):
            if a["role"] == "买家" and b["role"] == "客服":
                candidates.append((a["message_text"], b["message_text"]))

        # 选择 agent_reply 最长的配对（避免过场话）
        if candidates:
            buyer_msg, agent_msg = max(candidates, key=lambda x: len(x[1]))
            out.append(SimilarCase(
                session_id=sid, scene_minor=scene_minor,
                buyer_message=buyer_msg, agent_reply=agent_msg,
            ))

    return out
