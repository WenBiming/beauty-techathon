"""离线批处理流水线：L0 -> L1 -> L2，写库并产出成本报告。

L0 全量过一遍（零 token），L1 全量走 qwen3.8-flash，L2 只对高风险会话走
qwen3.7-plus。这个分层不是拍脑袋：能用 SQL 算准的信号绝不调模型，便宜
模型够用的不上贵模型（spec §4.2 / §4.5）。

成本单价不预设——按运行当日官方价目表传入。
"""
import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent import l1 as l1_mod
from agent import l2 as l2_mod
from agent import llm, promise, risk, rules
from core.clock import reference_now
from etl import db


@dataclass
class LayerCost:
    layer: str
    sessions: int = 0
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


@dataclass
class BatchReport:
    total_sessions: int = 0
    l1_degraded: int = 0
    l2_triggered: int = 0
    l2_degraded: int = 0
    promise_count: int = 0
    hard_promise_count: int = 0
    overdue_promise_count: int = 0
    risk_event_count: int = 0
    layers: list[LayerCost] = field(default_factory=list)


def _persist_summary(conn: sqlite3.Connection, r1, r2) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    actions = r2.suggested_actions if r2 is not None else []
    conn.execute(
        "INSERT OR REPLACE INTO session_summary"
        " (session_id, summary, scene_major, scene_minor, intent_confidence,"
        "  emotion, emotion_trend, risk_tags, suggested_actions, model,"
        "  tokens_in, tokens_out, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (r1.session_id, r1.summary, r1.scene_major, r1.scene_minor, r1.confidence,
         r1.emotion, None, json.dumps(r1.risk_tags, ensure_ascii=False),
         json.dumps(actions, ensure_ascii=False), r1.model,
         r1.tokens_in + (r2.tokens_in if r2 else 0),
         r1.tokens_out + (r2.tokens_out if r2 else 0), now),
    )


def _persist_buyer_risk_level(conn: sqlite3.Connection) -> int:
    """把每个买家的最高风险等级回填到 buyer_profile.risk_level（M1 预留的列）。

    注意执行顺序：etl.build 会整表重建 buyer_profile 并把 risk_level 写成
    NULL，所以本函数必须在 ETL 之后运行（M1 契约）。
    """
    levels = {}
    for r in conn.execute(
        "SELECT buyer, level FROM risk_event WHERE buyer IS NOT NULL"
    ):
        cur = levels.get(r["buyer"])
        if cur != "红":                      # 红 > 橙 > 无
            levels[r["buyer"]] = "红" if r["level"] == "红" else (cur or "橙")
    conn.executemany(
        "UPDATE buyer_profile SET risk_level = ? WHERE buyer = ?",
        [(lvl, b) for b, lvl in levels.items()],
    )
    conn.commit()
    return len(levels)


def run_batch(conn: sqlite3.Connection, client,
              session_ids: list[str] | None = None) -> BatchReport:
    if session_ids is None:
        session_ids = [r["session_id"] for r in conn.execute(
            "SELECT DISTINCT session_id FROM chat ORDER BY session_id")]

    l0 = LayerCost("L0")
    l1c = LayerCost("L1")
    l2c = LayerCost("L2")
    report = BatchReport(total_sessions=len(session_ids), layers=[l0, l1c, l2c])

    signals = {sid: rules.compute(conn, sid) for sid in session_ids}
    l0.sessions = len(signals)

    global_now = reference_now(conn)
    l1_results: dict[str, l1_mod.L1Result] = {}
    all_promises: list[promise.ResolvedPromise] = []

    for sid in session_ids:
        r1 = l1_mod.analyse(conn, client, sid)
        l1_results[sid] = r1
        l1c.sessions += 1
        l1c.calls += 1
        l1c.tokens_in += r1.tokens_in
        l1c.tokens_out += r1.tokens_out
        if r1.degraded:
            report.l1_degraded += 1

        ps = promise.evaluate(conn, sid, r1.promises, as_of=global_now)
        all_promises.extend(ps)

        r2 = None
        if l2_mod.should_trigger(signals[sid], r1):
            r2 = l2_mod.analyse(conn, client, sid, signals[sid], r1)
            l2c.sessions += 1
            l2c.calls += 1
            l2c.tokens_in += r2.tokens_in
            l2c.tokens_out += r2.tokens_out
            report.l2_triggered += 1
            if r2.degraded:
                report.l2_degraded += 1

        _persist_summary(conn, r1, r2)

    conn.executemany(
        "INSERT OR REPLACE INTO promise (message_id, session_id, buyer,"
        " promise_text, promise_type, made_at, deadline_at, ticket_no,"
        " closed, overdue) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(p.message_id, p.session_id, p.buyer, p.promise_text, p.promise_type,
          p.made_at, p.deadline_at, p.ticket_no, int(p.closed), int(p.overdue))
         for p in all_promises],
    )
    conn.commit()

    report.promise_count = len(all_promises)
    report.hard_promise_count = sum(1 for p in all_promises if p.promise_type == "hard")
    report.overdue_promise_count = sum(1 for p in all_promises if p.overdue)

    events = risk.detect(conn, signals, l1_results, all_promises)
    report.risk_event_count = risk.persist(conn, events)
    _persist_buyer_risk_level(conn)
    return report


def format_report(report: BatchReport,
                  prices: dict[str, tuple[float, float]] | None = None) -> str:
    lines = [
        "=== M2 批处理报告 ===",
        f"会话 {report.total_sessions} | L1 降级 {report.l1_degraded}"
        f" | L2 触发 {report.l2_triggered}（降级 {report.l2_degraded}）",
        f"承诺 {report.promise_count} 条（硬承诺 {report.hard_promise_count}，"
        f"逾期 {report.overdue_promise_count}）| 风险事件 {report.risk_event_count}",
        "",
        "层        会话   调用   输入token   输出token   合计token",
    ]
    for l in report.layers:
        lines.append(f"{l.layer:<9} {l.sessions:>4} {l.calls:>6} "
                     f"{l.tokens_in:>11} {l.tokens_out:>11} {l.tokens:>11}")
    total = sum(l.tokens for l in report.layers)
    lines.append(f"{'合计':<9} {'':>4} {'':>6} {'':>11} {'':>11} {total:>11}")

    if not prices:
        lines.append("\n（未提供单价，仅报 token。金额按运行当日官方价目表另算。）")
        return "\n".join(lines)

    def money(layer: LayerCost, model: str) -> float:
        pin, pout = prices.get(model, (0.0, 0.0))
        return layer.tokens_in / 1000 * pin + layer.tokens_out / 1000 * pout

    l1c = next(l for l in report.layers if l.layer == "L1")
    l2c = next(l for l in report.layers if l.layer == "L2")
    actual = money(l1c, l1_mod.MODEL) + money(l2c, l2_mod.MODEL)

    # 对照：假设全部会话都走 L2 模型，按 L2 实测的单次均量估算
    if l2c.calls:
        per_in = l2c.tokens_in / l2c.calls
        per_out = l2c.tokens_out / l2c.calls
    else:
        per_in, per_out = 0.0, 0.0
    baseline_layer = LayerCost("baseline", calls=report.total_sessions,
                               tokens_in=int(per_in * report.total_sessions),
                               tokens_out=int(per_out * report.total_sessions))
    baseline = money(baseline_layer, l2_mod.MODEL)
    saved = (1 - actual / baseline) * 100 if baseline else 0.0

    lines += [
        "",
        f"L0 规则层：¥0（{l0_zero(report)} 个会话零 token 过一遍）",
        f"实际成本：¥{actual:.4f}",
        f"对照（全量走 {l2_mod.MODEL}）：¥{baseline:.4f}",
        f"节省：{saved:.1f}%",
    ]
    return "\n".join(lines)


def l0_zero(report: BatchReport) -> int:
    return next(l.sessions for l in report.layers if l.layer == "L0")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M2 离线批处理流水线")
    ap.add_argument("--fixtures", action="store_true",
                    help="用录制好的 fixture 回放，不联网")
    ap.add_argument("--sessions", nargs="*", default=None,
                    help="只跑指定会话，默认全部 138 个")
    args = ap.parse_args(argv)

    client = llm.FixtureClient() if args.fixtures else llm.DashScopeClient()
    conn = db.connect()
    try:
        report = run_batch(conn, client, args.sessions)
    finally:
        conn.close()
    print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
