"""AI 生成话术的合规校验。**纯确定性逻辑，不调模型。**

背景：M2 跑完真实全量后实测 173 条生成话术——17% 编造买家性别称谓、
10% 编造客服身份、56% 新增时限承诺，还有 3 处编造订单号/物流号（其中
一条称「顺丰单号 SF12345678」，而该会话真实单号是圆通 YT7667875838478）。

spec §4.7 要求「事实性内容一律由工具从库里查出来，模型碰都不碰」，但
l2.build_context 把轨迹喂进去后没有任何机制阻止模型自己编。这一层就是
那个机制。

判级依据：
- 阻断：编造标识符。客服直接发出去就是对买家撒谎。
- 警告：编造称谓/身份。不算撒谎但会尴尬，客服知道实情时可自行修改。
- 提示：新增时限承诺。不是错误——客服本来就该做承诺——但要显式告诉他
        这条承诺会进入追踪，与本作品的承诺追踪功能形成闭环。
"""
import json
import re
import sqlite3
from dataclasses import dataclass

from etl.schema import TICKET_TABLES

KIND_FABRICATED_ID = "fabricated_id"
KIND_HONORIFIC = "invented_honorific"
KIND_NEW_PROMISE = "new_promise"

SEV_BLOCK = "阻断"
SEV_WARN = "警告"
SEV_INFO = "提示"

# 订单号 19 位纯数字；物流号 YT/SF+数字 或 12~15 位纯数字；工单号 5 种前缀
#
# 注意：**不能用 \b**。Python 的 \b 是 \w 与非 \w 的边界，而中文字符属于 \w，
# 所以「订单6920990836092915322的数据」里「单」和「6」之间没有边界，
# re.findall(r"\b\d{12,19}\b", ...) 会返回空 —— 而这正是实测中最常见的
# 编造形态。改用前后向断言，把中文当作合法边界，同时避免匹配长数字串的一截。
_ID_PATTERNS = [
    re.compile(r"(?<![0-9A-Za-z])\d{12,19}(?![0-9A-Za-z])"),
    re.compile(r"(?<![0-9A-Za-z])(?:YT|SF)\d{8,}(?![0-9A-Za-z])"),
    re.compile(r"(?<![0-9A-Za-z])(?:BLFY|BH|HV|WL|KOC)\d{6,}(?![0-9A-Za-z])"),
]

# 买家昵称已脱敏（如 邓e**），性别无从得知；客服也不是主管/经理
_HONORIFIC = re.compile(r"女士|先生|小姐|客服主管|主管|经理")

# 新增的时限承诺
_PROMISE = re.compile(
    r"\d+\s*(?:个)?\s*(?:小时|工作日|天|分钟)内|今天内|今日内|明早|明天之前|"
    r"第一时间|立刻|马上|一定|保证|承诺"
)


@dataclass(frozen=True)
class ComplianceIssue:
    kind: str
    severity: str
    detail: str
    excerpt: str


@dataclass(frozen=True)
class ComplianceReport:
    session_id: str
    tone: str
    text: str
    issues: list[ComplianceIssue]

    @property
    def blocked(self) -> bool:
        return any(i.severity == SEV_BLOCK for i in self.issues)


# 按库文件路径缓存。**不要改成另开一条连接去查**——若传入内存库，
# PRAGMA database_list 返回空路径，sqlite3.connect("") 会建一个全新的空库，
# known_identifiers 静默返回空集合，校验器变成永不报错的空壳，且没有任何
# 测试会失败。一律用传入的 conn 直查。
_ID_CACHE: dict[str, frozenset[str]] = {}


def _collect_identifiers(conn: sqlite3.Connection) -> frozenset[str]:
    ids: set[str] = set()
    for r in conn.execute("SELECT order_no, tracking_no FROM orders"):
        ids.add(r["order_no"])
        if r["tracking_no"]:
            ids.add(r["tracking_no"])
    for table in TICKET_TABLES:
        cols = {c[1] for c in conn.execute(f"PRAGMA table_info({table})")}
        for r in conn.execute(f"SELECT * FROM {table}"):
            ids.add(r["ticket_no"])
            for col in ("tracking_no", "orig_tracking_no",
                        "reissue_tracking_no"):
                if col in cols and r[col]:
                    ids.add(r[col])
    return frozenset(i for i in ids if i)


def known_identifiers(conn: sqlite3.Connection) -> frozenset[str]:
    """库内全部真实订单号/物流号/工单号。用传入的 conn 直查，按库路径缓存。

    标识符来自原表，只有重跑 ETL 才会变，所以进程内缓存是安全的。
    路径为空（内存库）时不缓存，每次直查。
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    path = row[2] if row is not None else ""
    if not path:
        return _collect_identifiers(conn)
    if path not in _ID_CACHE:
        _ID_CACHE[path] = _collect_identifiers(conn)
    return _ID_CACHE[path]


def check_reply(conn: sqlite3.Connection, session_id: str, tone: str,
                 text: str) -> ComplianceReport:
    issues: list[ComplianceIssue] = []
    real = known_identifiers(conn)

    seen: set[str] = set()
    for pat in _ID_PATTERNS:
        for m in pat.findall(text or ""):
            if m in seen or m in real:
                continue
            seen.add(m)
            issues.append(ComplianceIssue(
                kind=KIND_FABRICATED_ID, severity=SEV_BLOCK,
                detail="话术中的单号在业务库里查不到，疑似模型编造",
                excerpt=m,
            ))

    for m in dict.fromkeys(_HONORIFIC.findall(text or "")):
        issues.append(ComplianceIssue(
            kind=KIND_HONORIFIC, severity=SEV_WARN,
            detail="买家昵称已脱敏、客服身份未知，该称谓无数据支持",
            excerpt=m,
        ))

    for m in dict.fromkeys(_PROMISE.findall(text or "")):
        issues.append(ComplianceIssue(
            kind=KIND_NEW_PROMISE, severity=SEV_INFO,
            detail="这是一条新承诺，发出后将进入承诺追踪",
            excerpt=m,
        ))

    return ComplianceReport(session_id=session_id, tone=tone, text=text,
                             issues=issues)


def check_session(conn: sqlite3.Connection,
                   session_id: str) -> list[ComplianceReport]:
    row = conn.execute(
        "SELECT replies FROM session_summary WHERE session_id = ?",
        (session_id,)).fetchone()
    if row is None or not row["replies"]:
        return []
    return [check_reply(conn, session_id, x.get("tone", ""), x.get("text", ""))
            for x in json.loads(row["replies"])]
