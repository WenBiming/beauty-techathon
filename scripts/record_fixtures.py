"""录制真实模型响应为 fixture。**这是唯一会联网的脚本。**

用法：
    .venv/bin/python -m scripts.record_fixtures S00005 S00059 S00099 S00362

录完的 fixture 落在 tests/fixtures/llm/，测试用 FixtureClient 回放，
从此不再联网。
"""
import sys

from agent import l1, llm
from etl import db


def main(session_ids: list[str]) -> int:
    conn = db.connect()
    client = llm.RecordingClient(llm.DashScopeClient())
    total_in = total_out = 0
    try:
        for sid in session_ids:
            r = l1.analyse(conn, client, sid)
            total_in += r.tokens_in
            total_out += r.tokens_out
            flag = "降级" if r.degraded else f"{r.scene_minor} / 情绪{r.emotion}"
            print(f"{sid}  {flag}  承诺{len(r.promises)}条  "
                  f"token in={r.tokens_in} out={r.tokens_out}")
    finally:
        conn.close()
    print(f"\n合计 token: in={total_in} out={total_out} 总计={total_in + total_out}")
    print(f"fixture 写入 {llm.FIXTURE_DIR}")
    return 0


if __name__ == "__main__":
    ids = sys.argv[1:] or ["S00005", "S00059", "S00099", "S00362"]
    sys.exit(main(ids))
