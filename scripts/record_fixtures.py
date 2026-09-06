"""录制真实模型响应为 fixture。**这是唯一会联网的脚本。**

用法：
    .venv/bin/python -m scripts.record_fixtures S00005 S00059 S00099 S00362

录完的 fixture 落在 tests/fixtures/llm/，测试用 FixtureClient 回放，
从此不再联网。

除了 L1，对触发 L2 的会话也录一次 L2 的 fixture（判定逻辑与 pipeline 一致：
agent.l2.should_trigger），这样这些会话的完整链路都能离线回放。
"""
import sys

from agent import l1, l2, llm, rules
from etl import db


def main(session_ids: list[str]) -> int:
    conn = db.connect()
    client = llm.RecordingClient(llm.DashScopeClient())
    total_in = total_out = 0
    try:
        for sid in session_ids:
            signals = rules.compute(conn, sid)
            r = l1.analyse(conn, client, sid)
            total_in += r.tokens_in
            total_out += r.tokens_out
            flag = "降级" if r.degraded else f"{r.scene_minor} / 情绪{r.emotion}"
            print(f"{sid}  {flag}  承诺{len(r.promises)}条  "
                  f"token in={r.tokens_in} out={r.tokens_out}")

            if l2.should_trigger(signals, r):
                r2 = l2.analyse(conn, client, sid, signals, r)
                total_in += r2.tokens_in
                total_out += r2.tokens_out
                flag2 = "降级" if r2.degraded else f"话术{len(r2.replies)}条"
                print(f"  └─ L2 触发  {flag2}  "
                      f"token in={r2.tokens_in} out={r2.tokens_out}")
    finally:
        conn.close()
    print(f"\n合计 token: in={total_in} out={total_out} 总计={total_in + total_out}")
    print(f"fixture 写入 {llm.FIXTURE_DIR}")
    return 0


if __name__ == "__main__":
    ids = sys.argv[1:] or ["S00005", "S00059", "S00099", "S00362"]
    sys.exit(main(ids))
