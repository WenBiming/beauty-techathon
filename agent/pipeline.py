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
    """写 session_summary。

    L2 的三项产出必须全部落库（I1）：suggested_actions 之外，risk_attribution
    喂 spec §5.2 卡片③、replies 喂卡片④（共情话术，一键插入）。算完就扔意味着
    M3 要么重花一次钱、要么改成在线实时生成（违背 §4.8）。

    model 写 JSON 数组而不是单个模型名（I3）：此前写 r1.model 但 token 记的是
    r1+r2 的和，成本看板按这一列查价目表会把 plus 的 token 按 flash 单价计，
    低估约 3 倍。分层 token 另存四列，合计列保持不变。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    actions = r2.suggested_actions if r2 is not None else []
    replies = [{"tone": x.tone, "text": x.text} for x in r2.replies] if r2 else []
    models = [r1.model] + ([r2.model] if r2 is not None else [])
    conn.execute(
        "INSERT OR REPLACE INTO session_summary"
        " (session_id, summary, scene_major, scene_minor, intent_confidence,"
        "  emotion, emotion_trend, risk_tags, suggested_actions,"
        "  risk_attribution, replies, model, tokens_in, tokens_out,"
        "  l1_tokens_in, l1_tokens_out, l2_tokens_in, l2_tokens_out, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (r1.session_id, r1.summary, r1.scene_major, r1.scene_minor, r1.confidence,
         r1.emotion, None, json.dumps(r1.risk_tags, ensure_ascii=False),
         json.dumps(actions, ensure_ascii=False),
         r2.risk_attribution if r2 is not None else None,
         json.dumps(replies, ensure_ascii=False),
         json.dumps(models, ensure_ascii=False),
         r1.tokens_in + (r2.tokens_in if r2 else 0),
         r1.tokens_out + (r2.tokens_out if r2 else 0),
         str(r1.tokens_in), str(r1.tokens_out),
         str(r2.tokens_in) if r2 is not None else None,
         str(r2.tokens_out) if r2 is not None else None,
         now),
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


def _backfill_emotion_trend(conn: sqlite3.Connection, session_ids: list[str],
                            l1_results: dict) -> int:
    """回填 session_summary.emotion_trend（I2）。

    spec §5.2 卡片①「情绪条 + 较上次会话的趋势箭头」是第一屏不可折叠内容，
    数据本来就齐：同买家的上一次会话可查，其 emotion 也已算出。

    必须在所有 L1 跑完之后统一回填，不能边跑边写——同批次内先处理的会话
    也可能是后处理会话的「上一次」，边跑边写会漏掉这一半。
    无上一次会话写 NULL，其余写 上升 / 下降 / 持平。
    """
    filled = 0
    for sid in session_ids:
        first = conn.execute(
            "SELECT buyer, MIN(sent_at) AS t FROM chat WHERE session_id = ?",
            (sid,),
        ).fetchone()
        if first is None or not first["t"]:
            continue
        prior = conn.execute(
            "SELECT session_id, MAX(sent_at) AS t FROM chat"
            " WHERE buyer = ? AND sent_at < ? GROUP BY session_id"
            " ORDER BY t DESC LIMIT 1",
            (first["buyer"], first["t"]),
        ).fetchone()
        if prior is None or not prior["session_id"]:
            continue
        prior_id = prior["session_id"]
        r_prior = l1_results.get(prior_id)
        if r_prior is not None:
            prior_emotion = r_prior.emotion
        else:                       # 上一次会话不在本批次，读已落库的结果
            row = conn.execute(
                "SELECT emotion FROM session_summary WHERE session_id = ?",
                (prior_id,),
            ).fetchone()
            prior_emotion = row["emotion"] if row is not None else None
        cur = l1_results.get(sid)
        if prior_emotion is None or cur is None:
            continue
        # emotion 是 1-5 分，分越高越平静：现在比上次高 = 情绪好转 = 上升
        if cur.emotion > prior_emotion:
            trend = "上升"
        elif cur.emotion < prior_emotion:
            trend = "下降"
        else:
            trend = "持平"
        conn.execute(
            "UPDATE session_summary SET emotion_trend = ? WHERE session_id = ?",
            (trend, sid),
        )
        filled += 1
    conn.commit()
    return filled


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
        # 逾期承诺参与 L2 触发（I4）：ps 就在手边，spec §4.4 点名的这一类
        # 此前从不触发深度分析。
        if l2_mod.should_trigger(signals[sid], r1,
                                 has_overdue_promise=any(p.overdue for p in ps)):
            r2 = l2_mod.analyse(conn, client, sid, signals[sid], r1)
            l2c.sessions += 1
            l2c.calls += 1
            l2c.tokens_in += r2.tokens_in
            l2c.tokens_out += r2.tokens_out
            report.l2_triggered += 1
            if r2.degraded:
                report.l2_degraded += 1

        _persist_summary(conn, r1, r2)

    _backfill_emotion_trend(conn, session_ids, l1_results)

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

    l0_sessions = next(l.sessions for l in report.layers if l.layer == "L0")
    lines += [
        "",
        f"L0 规则层：¥0（{l0_sessions} 个会话零 token 过一遍）",
        f"实际成本：¥{actual:.4f}",
    ]
    if baseline:
        saved = (1 - actual / baseline) * 100
        lines += [
            f"对照（全量走 {l2_mod.MODEL}）：¥{baseline:.4f}",
            f"节省：{saved:.1f}%",
        ]
    else:
        # baseline 为 0 说明本批次一次 L2 都没触发，没有单次均量可外推。
        # 打印「对照 ¥0.0000 / 节省 0.0%」比不打印更误导（I8）。
        lines.append("（本批次 L2 零调用，无单次均量可外推，跳过全量对照。）")
    return "\n".join(lines)


def parse_price(spec: str) -> tuple[str, tuple[float, float]]:
    """解析 --price 的 MODEL:IN:OUT（每千 token 单价，元）。"""
    parts = spec.split(":")
    if len(parts) != 3:
        raise ValueError(f"--price 格式应为 MODEL:IN:OUT，得到 {spec!r}")
    model, pin, pout = parts
    if not model:
        raise ValueError(f"--price 缺少模型名：{spec!r}")
    return model, (float(pin), float(pout))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M2 离线批处理流水线")
    ap.add_argument("--fixtures", action="store_true",
                    help="用录制好的 fixture 回放，不联网")
    ap.add_argument("--sessions", nargs="*", default=None,
                    help="只跑指定会话，默认全部 138 个")
    # 没有这个入口的话，format_report 的整个金额分支只有测试能到达，
    # 而 spec §4.5 的成本对比表是明文加分项、要进 PPT（I8）。
    ap.add_argument("--price", action="append", default=[], metavar="MODEL:IN:OUT",
                    help="模型每千 token 单价（元），可重复。"
                         "例：--price qwen3.8-flash:0.0003:0.0006")
    args = ap.parse_args(argv)

    prices = dict(parse_price(s) for s in args.price)

    client = llm.FixtureClient() if args.fixtures else llm.DashScopeClient()
    conn = db.connect()
    try:
        # 幂等，且会走 _migrate_columns 给已存在的库补上新增的派生列
        # （session_summary 的 risk_attribution / replies / 分层 token）。
        db.create_tables(conn)
        report = run_batch(conn, client, args.sessions)
    finally:
        conn.close()
    print(format_report(report, prices or None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
