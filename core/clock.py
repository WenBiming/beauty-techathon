"""参考时钟。

数据集时间范围是 2026-05-05 ~ 05-23，而系统运行在真实当下。「工单超期」
「承诺逾期」都需要一个「现在」作基准，取错会让预警失去区分度（spec §4.2.1）：

- 插件（会话视角）用情景时钟：now = 该会话最后一条消息时间，
  这是客服接入那一刻真实看到的状态。
- 看板（主管视角）用全局时钟：now = 数据集末尾。

生产环境把 reference_now 换成真实 datetime.now() 即可，业务逻辑不用改。
"""
import sqlite3
from datetime import datetime, timedelta

GLOBAL_NOW = "2026-05-23 10:04:48"


def reference_now(conn: sqlite3.Connection, session_id: str | None = None) -> datetime:
    """情景时钟（传 session_id）或全局时钟（不传）。未知会话回退到全局时钟。"""
    if session_id is not None:
        row = conn.execute(
            "SELECT MAX(sent_at) AS t FROM chat WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is not None and row["t"]:
            return datetime.fromisoformat(row["t"])
    return datetime.fromisoformat(GLOBAL_NOW)


def add_business_days(start: datetime, n: int) -> datetime:
    """从 start 往后推 n 个工作日，跳过周六周日。n <= 0 原样返回。

    承诺解析用它把「3 个工作日内」换算成绝对 deadline。
    """
    if n <= 0:
        return start
    cur = start
    left = n
    while left > 0:
        cur += timedelta(days=1)
        if cur.weekday() < 5:      # 0=周一 ... 4=周五
            left -= 1
    return cur
