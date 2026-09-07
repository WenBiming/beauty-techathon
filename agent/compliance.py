"""AI 生成话术的合规校验。**纯确定性逻辑，不调模型。**

背景：M2 跑完真实全量后实测 173 条生成话术——17% 编造买家性别称谓、
10% 编造客服身份、56% 新增时限承诺，还有 3 处编造订单号/物流号（其中
一条称「顺丰单号 SF12345678」，而该会话真实单号是圆通 YT7667875838478）。

spec §4.7 要求「事实性内容一律由工具从库里查出来，模型碰都不碰」，但
l2.build_context 把轨迹喂进去后没有任何机制阻止模型自己编。这一层就是
那个机制。

判级依据：
- 阻断：编造标识符（查无此号），以及**真实但不属于本会话买家**的标识符。
        客服直接发出去就是对买家撒谎，后者还额外泄露另一买家的物流信息。
- 警告：编造称谓/身份。不算撒谎但会尴尬，客服知道实情时可自行修改。
- 提示：新增**硬时限**承诺。不是错误——客服本来就该做承诺——但要显式告诉
        他这条承诺会进入追踪，与本作品的承诺追踪功能形成闭环。
"""
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass

from etl.schema import TICKET_TABLES

KIND_FABRICATED_ID = "fabricated_id"
KIND_FOREIGN_ID = "foreign_id"
KIND_HONORIFIC = "invented_honorific"
KIND_NEW_PROMISE = "new_promise"

SEV_BLOCK = "阻断"
SEV_WARN = "警告"
SEV_INFO = "提示"

# 工单号前缀所在的物流号列（各工单表列名不一）
_TICKET_TRACKING_COLS = ("tracking_no", "orig_tracking_no", "reissue_tracking_no")

# 订单号 19 位纯数字；物流号 YT/SF+数字 或 12~15 位纯数字；工单号 5 种前缀
#
# 注意：**不能用 \b**。Python 的 \b 是 \w 与非 \w 的边界，而中文字符属于 \w，
# 所以「订单6920990836092915322的数据」里「单」和「6」之间没有边界，
# re.findall(r"\b\d{12,19}\b", ...) 会返回空 —— 而这正是实测中最常见的
# 编造形态。改用前后向断言，把中文当作合法边界，同时避免匹配长数字串的一截。
#
# 匹配跑在 _normalize() 之后的文本上（NFKC + 去空格/连字符 + 转大写），
# 所以 `sf1234567890`、`SF-1234567890`、`SF 1234567890`、全角数字都能命中。
# IGNORECASE 是双保险。
_ID_PATTERNS = [
    re.compile(r"(?<![0-9A-Za-z])\d{12,19}(?![0-9A-Za-z])"),
    re.compile(r"(?<![0-9A-Za-z])(?:YT|SF)\d{8,}(?![0-9A-Za-z])", re.IGNORECASE),
    re.compile(r"(?<![0-9A-Za-z])(?:BLFY|BH|HV|WL|KOC)\d{6,}(?![0-9A-Za-z])",
               re.IGNORECASE),
]

# 买家昵称已脱敏（如 邓e**），性别无从得知；客服也不是主管/经理。
# 只匹配「自称」——第三方身份（「我联系了仓库主管」）是合理信息，不该报警。
# 「小姐」加 (?!姐)：语料里有「催财务小姐姐立刻操作退款」，那是对第三方同事的
# 口语称呼，与「仓库主管」是同一类误报。
_HONORIFIC = re.compile(
    r"女士|先生|小姐(?!姐)|我是[^，。！？]{0,6}?(?:主管|经理)|"
    r"本人是[^，。！？]{0,6}?(?:主管|经理)"
)

# 新增的**硬时限**承诺：带可核算 deadline 的表述。只有这一组出 issue。
#
# 口径与 agent/promise.py 的 hard/soft 分级对齐：promise.py 的模块 docstring
# 已裁决「软承诺（amount 为 None，如「马上帮您查」）只记录不预警，否则误报会
# 淹没真信号」。合规侧若把「马上」「一定」与「48小时内」同级推给客服，57% 的
# 话术会挂同一枚徽章，真信号被噪声淹没——同一知识两处实现且分叉。
#
# 周期性时限（「每 4 小时同步一次」）也算硬承诺：
# **与 agent/l2.py 的 SYSTEM 提示词第 4 条（禁止凭空承诺「今天内」「每 4 小时」）
# 是同一份约束的两处实现，改动其一必须同步另一处。**
_PROMISE_HARD = re.compile(
    r"每\s*\d+\s*(?:个)?\s*(?:小时|工作日|天|分钟)|"
    r"\d+\s*(?:个)?\s*(?:小时|工作日|天|分钟)内|"
    r"今天内|今日内|明早|明天之前"
)

# 软承诺词表。**故意不出 issue**，留在这里是为了说明「这些不报是裁决 R4 的
# 结果，不是漏了」。抽取侧见 agent/promise.py 的 promise_type == "soft"。
_PROMISE_SOFT = re.compile(r"第一时间|立刻|马上|一定|保证|承诺")

# 过往回指标记：命中所在**小句**含这些词时，说的是「过去那条承诺」而不是新承诺。
# 例如「未能按承诺在 48 小时内发出，这是我们的疏忽」。
_PAST_REFERENCE = re.compile(r"[未没]能|[未没]按|没有|超时|逾期|之前|上次|已经")

# 小句切分符。**逗号必须切**：实测在整句粒度上，「我已经向仓库主管申请了加急，
# 您的补发包裹将在今天内发出」会因为前半句的「已经」被整条排除——那是一条真
# 新承诺，漏掉它就等于漏掉一次承诺追踪。真正的过往回指（「未能按承诺在48小时
# 内发出」「之前答应您48小时内发出」）标记与命中总在同一个小句里。
_CLAUSE_SPLIT = "，,、。！？!?；;：:\n"


@dataclass(frozen=True)
class ComplianceIssue:
    kind: str
    severity: str
    detail: str
    excerpt: str
    # 命中片段在**原始话术**中的下标（左闭右开），供 M3b 卡片④高亮。
    # 同一片段多次出现时只记首次出现的位置——不然一条话术会刷出十几个
    # 同样的 issue，把卡片淹掉。
    start: int = -1
    end: int = -1


@dataclass(frozen=True)
class ComplianceReport:
    session_id: str
    tone: str
    text: str
    issues: list[ComplianceIssue]

    @property
    def blocked(self) -> bool:
        return any(i.severity == SEV_BLOCK for i in self.issues)


def _normalize(text: str) -> tuple[str, list[int]]:
    """归一化文本，并返回「归一化下标 -> 原文下标」的映射。

    做三件事：NFKC（全角数字/字母 -> 半角）、去空格与连字符、转大写。
    这样 `sf1234567890`、`SF-1234567890`、`SF 1234567890`、`ＳＦ１２…`
    都会归一到同一形态，不再静默漏报——漏报是本作品最贵的那种错。

    逐字符归一化（而不是整串 normalize）是为了保住下标映射：ComplianceIssue
    的 start/end 必须指回**原文**，否则高亮会错位。
    """
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text or ""):
        for c in unicodedata.normalize("NFKC", ch):
            if c in " \t　-­‐‑‒–—":
                continue
            out.append(c.upper())
            index.append(i)
    return "".join(out), index


def normalize_identifier(value: str) -> str:
    """标识符归一化。白名单与话术必须走同一条路径。

    ⚠️ 白名单不做同样归一化的话，真实单号被写成小写就会被判成编造并阻断——
    比漏报更糟，客服会不信任这个校验器。
    """
    return _normalize(value or "")[0]


# 按库文件路径缓存。**不要改成另开一条连接去查**——若传入内存库，
# PRAGMA database_list 返回空路径，sqlite3.connect("") 会建一个全新的空库，
# known_identifiers 静默返回空集合，校验器变成永不报错的空壳，且没有任何
# 测试会失败。一律用传入的 conn 直查。
_ID_CACHE: dict[str, frozenset[str]] = {}
_BUYER_ID_CACHE: dict[str, dict[str, frozenset[str]]] = {}


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
            for col in _TICKET_TRACKING_COLS:
                if col in cols and r[col]:
                    ids.add(r[col])
    return frozenset(i for i in ids if i)


def _collect_by_buyer(conn: sqlite3.Connection) -> dict[str, frozenset[str]]:
    """买家昵称 -> 该买家名下全部标识符。会话号也当 key 收一份（买家字段为空时兜底）。"""
    acc: dict[str, set[str]] = {}

    def add(keys, value):
        if not value:
            return
        for k in keys:
            if k:
                acc.setdefault(k, set()).add(value)

    for r in conn.execute("SELECT order_no, tracking_no, buyer, session_id FROM orders"):
        keys = (r["buyer"], r["session_id"])
        add(keys, r["order_no"])
        add(keys, r["tracking_no"])
    for table in TICKET_TABLES:
        cols = {c[1] for c in conn.execute(f"PRAGMA table_info({table})")}
        for r in conn.execute(f"SELECT * FROM {table}"):
            keys = (r["buyer"], r["session_id"])
            add(keys, r["ticket_no"])
            for col in _TICKET_TRACKING_COLS:
                if col in cols and r[col]:
                    add(keys, r[col])
    return {k: frozenset(v) for k, v in acc.items()}


def _db_path(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    return row[2] if row is not None else ""


def known_identifiers(conn: sqlite3.Connection) -> frozenset[str]:
    """库内全部真实订单号/物流号/工单号。用传入的 conn 直查，按库路径缓存。

    标识符来自原表，只有重跑 ETL 才会变，所以进程内缓存是安全的。
    路径为空（内存库）时不缓存，每次直查。
    """
    path = _db_path(conn)
    if not path:
        return _collect_identifiers(conn)
    if path not in _ID_CACHE:
        _ID_CACHE[path] = _collect_identifiers(conn)
    return _ID_CACHE[path]


def buyer_identifiers(conn: sqlite3.Connection, session_id: str) -> frozenset[str]:
    """**本会话买家**名下的真实标识符。

    白名单必须按买家收窄：全库 335 个标识符的并集里，别人的单号一样「查得到」，
    校验器会放行。实测 S00099 的话术写「您的订单6920012167512343101已发出」
    不报警，而那个订单号属于 S00156 的另一个买家——这比编造单号更危险：
    编造的号在快递官网查无此单，这个号查得出，查出的是别人的包裹，同时泄露
    另一买家的物流信息。暴露路径真实存在（retrieval.search_similar_cases 会把
    其它会话的客服原话逐字塞进 L2 的 few-shot，而库内有 5 条客服消息带真实运单号）。
    """
    path = _db_path(conn)
    if not path:
        by_buyer = _collect_by_buyer(conn)
    else:
        if path not in _BUYER_ID_CACHE:
            _BUYER_ID_CACHE[path] = _collect_by_buyer(conn)
        by_buyer = _BUYER_ID_CACHE[path]

    ids: set[str] = set(by_buyer.get(session_id, ()))
    row = conn.execute(
        "SELECT buyer FROM chat WHERE session_id = ? AND buyer IS NOT NULL"
        " AND buyer != '' LIMIT 1", (session_id,)).fetchone()
    if row is not None and row["buyer"]:
        ids |= set(by_buyer.get(row["buyer"], ()))
    return frozenset(ids)


def _clause_at(text: str, pos: int) -> str:
    """取 pos 所在的小句（原文）。用于过往回指判定。"""
    start = 0
    for i in range(pos - 1, -1, -1):
        if text[i] in _CLAUSE_SPLIT:
            start = i + 1
            break
    end = len(text)
    for i in range(pos, len(text)):
        if text[i] in _CLAUSE_SPLIT:
            end = i
            break
    return text[start:end]


def _spans(pattern: re.Pattern, norm: str, index: list[int],
           text: str) -> list[tuple[str, int, int]]:
    """在归一化文本上找命中，映射回原文的 (excerpt, start, end)。按片段去重，保留首次位置。"""
    out: list[tuple[str, int, int]] = []
    seen: set[str] = set()
    for m in pattern.finditer(norm):
        key = m.group(0)
        if key in seen:
            continue
        seen.add(key)
        start = index[m.start()]
        end = index[m.end() - 1] + 1
        out.append((text[start:end], start, end))
    return out


def check_reply(conn: sqlite3.Connection, session_id: str, tone: str,
                 text: str) -> ComplianceReport:
    issues: list[ComplianceIssue] = []
    text = text or ""
    norm, index = _normalize(text)

    all_real = {normalize_identifier(i) for i in known_identifiers(conn)}
    mine = {normalize_identifier(i) for i in buyer_identifiers(conn, session_id)}

    seen_ids: set[str] = set()
    for pat in _ID_PATTERNS:
        for m in pat.finditer(norm):
            key = m.group(0)
            if key in seen_ids or key in mine:
                continue
            seen_ids.add(key)
            start = index[m.start()]
            end = index[m.end() - 1] + 1
            if key in all_real:
                issues.append(ComplianceIssue(
                    kind=KIND_FOREIGN_ID, severity=SEV_BLOCK,
                    detail="该单号真实存在但不属于本会话买家，发出会泄露他人物流信息",
                    excerpt=text[start:end], start=start, end=end,
                ))
            else:
                issues.append(ComplianceIssue(
                    kind=KIND_FABRICATED_ID, severity=SEV_BLOCK,
                    detail="话术中的单号在业务库里查不到，疑似模型编造",
                    excerpt=text[start:end], start=start, end=end,
                ))

    for excerpt, start, end in _spans(_HONORIFIC, norm, index, text):
        issues.append(ComplianceIssue(
            kind=KIND_HONORIFIC, severity=SEV_WARN,
            detail="买家昵称已脱敏、客服身份未知，该称谓无数据支持",
            excerpt=excerpt, start=start, end=end,
        ))

    for excerpt, start, end in _spans(_PROMISE_HARD, norm, index, text):
        if _PAST_REFERENCE.search(_clause_at(text, start)):
            continue        # 对过往破约的道歉回指，不是新承诺
        issues.append(ComplianceIssue(
            kind=KIND_NEW_PROMISE, severity=SEV_INFO,
            detail="这是一条新的硬时限承诺，发出后将进入承诺追踪",
            excerpt=excerpt, start=start, end=end,
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
