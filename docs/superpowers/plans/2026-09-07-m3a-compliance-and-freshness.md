# M3a 话术合规校验与数据新鲜度 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 AI 生成的共情话术加一道确定性的合规校验（禁止编造订单号/物流号/身份称谓、显式标出新增承诺），并修掉批处理产出的陈旧行累积问题，让 M3b 的插件与看板拿到干净、可信的数据。

**Architecture:** 合规校验是**纯确定性逻辑**，不调模型——把库里 290 个真实标识符当白名单，正则扫出话术里的标识符/称谓/时限承诺，逐条判级。陈旧行用 `last_batch_at` 标记而非删除（删除会洗掉主管的处置状态，那正是 M2 裁决 C1 要避免的）。最后加固 L2 提示词并重跑，用加固前后的合规率对比作为实证。

**Tech Stack:** Python 3.12 · SQLite · pytest（沿用 M1/M2 栈，不引入新依赖）

**Spec:** `docs/superpowers/specs/2026-09-02-beauty-techathon-empathy-agent-design.md`（重点 §4.7 幻觉控制、§5.1 铁律「AI 只建议不发送」、§5.2 卡片④）

## 为什么要做这件事（实测证据）

M2 跑完真实全量后，我扫了 60 个会话产出的 **173 条共情话术**：

| 问题 | 实测 | 例子 |
|---|---|---|
| 编造买家性别称谓 | **30 条（17%）** | 「邓女士您好」——数据里只有 `邓e**` 这样的脱敏昵称，性别无从得知 |
| 编造客服身份 | **19 条（10%）** | 「我是客服主管」——客服不是主管 |
| 新增时限承诺 | **97 条（56%）** | 「今天内会有明确结果」「每4小时监控一次」 |
| **编造标识符** | **3 处** | S00006 称「顺丰单号是 SF12345678…」，而该会话真实单号是**圆通 YT7667875838478** |

spec §4.7 写的是「事实性内容一律由工具从库里查出来，模型碰都不碰」，但 `l2.build_context` 把轨迹喂进去之后，没有任何机制阻止模型自己编。

最讽刺的一条：**本作品做「承诺追踪」来发现客服没兑现的承诺，而我们的 AI 自己在生成 56% 带时限的新承诺。**

评审若对着演示视频里的话术查一下订单号，会当场发现是假的。这不只是质量问题，是对作品核心主张的正面否定。

## Global Constraints

- Python 3.12；解释器 `/Users/wenbiming/dev/beauty-techathon/.venv/bin/python`
- **合规校验不调模型**：`agent/compliance.py` 不得 import `agent.llm` 或 openai。确定性、可测、零成本是它的全部价值。
- **测试永不联网**；**写库的测试必须用隔离临时库**（`tmp_path_factory` + 全量 ETL，0.3 秒）
- **不得删除 `promise` / `risk_event` 的行**——M2 裁决 C1 明确要求保住主管的 `status`/`handler` 标记
- 模型 L1=`qwen3.8-flash` / L2=`qwen3.7-plus`；所有真实请求带 `enable_thinking: False`
- 成本单价不预设
- 密钥绝不入库
- YAGNI：不做话术自动改写、不做敏感词库、不做多语言、不引入 NLP 库

## M2 已交付、可直接用的接口

```python
# etl/db.py
def connect(path=None) -> sqlite3.Connection      # row_factory = sqlite3.Row
def create_tables(conn) -> None                   # 含 _migrate_columns 自动补列
DERIVED_COLUMNS: dict[str, dict[str, str]]        # 派生表列的单一事实来源，加列改这里

# etl/schema.py   TABLES; TICKET_TABLES; CLOSED_STATUS = "已完结"
# core/clock.py   reference_now(conn, session_id=None); add_business_days(start, n)
# agent/risk.py   RiskEvent; RISK_TYPES; detect(conn, signals, l1_results, promises); persist(conn, events)
# agent/promise.py  RawPromise; ResolvedPromise; is_overdue_at(p, as_of); evaluate(conn, sid, raws, as_of)
# agent/l2.py     MODEL="qwen3.7-plus"; SYSTEM; Reply(tone, text); L2Result; should_trigger(...); build_context(...); analyse(...)
# agent/pipeline.py  run_batch(conn, client, session_ids=None) -> BatchReport; format_report(report, prices=None)
```

`session_summary` 现有列（全 TEXT）：
`session_id, summary, scene_major, scene_minor, intent_confidence, emotion, emotion_trend, high_risk, risk_tags, suggested_actions, risk_attribution, replies, model, tokens_in, tokens_out, l1_tokens_in, l1_tokens_out, l2_tokens_in, l2_tokens_out, updated_at`

- `replies` 是 JSON 数组，每项 `{"tone": "...", "text": "..."}`
- `high_risk` 存的是字符串 `'true'`/`'false'`，读时要解析

## 事实基线（测试断言引用这些实测数字）

| 项 | 值 |
|---|---|
| 库内真实标识符总数（订单号+物流号+工单号，含工单表的原单号/补发单号） | **335** |
| 订单号格式 | 19 位纯数字，如 `6920947277927059788` |
| 物流号格式 | `YT`/`SF` + 数字，或 12~15 位纯数字，如 `773415850905356` |
| 工单号前缀 | `BH`(补发换货) `HV`(线下打款) `WL`(物流) `BLFY`(不良反应) `KOC`(售后退货) |
| 有话术的会话 / 话术条数 | 60 / 173 |
| `promise` 表行数 vs 本次批处理产出 | 221 vs 172（约 49 行陈旧） |
| `risk_event` 表行数 | 129，其中 4 行来自更早一次运行 |
| `risk_event` 已有人工处置的行 | 1 行（`status='已闭环'`, `handler='主管小李'`，M2 验证 C1 时留下的） |

## File Structure

| 文件 | 职责 |
|---|---|
| `agent/compliance.py` | 话术合规校验：标识符白名单、称谓检测、新承诺抽取。纯逻辑，不调模型。 |
| `etl/db.py`（改） | `promise` / `risk_event` 加 `last_batch_at` 列 |
| `agent/risk.py`（改） | `persist` 写 `last_batch_at` |
| `agent/pipeline.py`（改） | 生成批次时间戳、写入 promise 的 `last_batch_at`、批处理报告加合规统计 |
| `agent/l2.py`（改） | `SYSTEM` 提示词加固：禁止编造事实与身份 |
| `tests/test_compliance.py` | 合规校验测试 |
| `tests/test_freshness.py` | 陈旧行标记测试 |

---

### Task 1: 数据新鲜度标记

**Files:**
- Modify: `etl/db.py`（`DERIVED_COLUMNS` 加列）
- Modify: `agent/risk.py`（`persist` 写 `last_batch_at`）
- Modify: `agent/pipeline.py`（生成批次戳、传给 persist、写 promise）
- Test: `tests/test_freshness.py`

**Interfaces:**
- Consumes: `etl.db.connect` / `create_tables`、`agent.risk.persist`、`agent.pipeline.run_batch`
- Produces:
  - `promise` 与 `risk_event` 表新增列 `last_batch_at TEXT`
  - `agent.pipeline.current_batch_at(conn) -> str | None` — 返回库内最新批次戳（两表取较大者），无数据返回 `None`
  - `agent.risk.persist(conn, events, batch_at: str) -> int` — 签名新增第三个参数

**背景**：`promise` 与 `risk_event` 是**只增不删**语义（M2 裁决 C1：为保住主管在看板上标记的 `status`/`handler`，`persist` 从 `INSERT OR REPLACE` 改成了 upsert）。代价是上次批处理留下、本次条件已不成立的行不会被清理。

实测：`promise` 表 221 行但本次批处理只产出 172 条；`risk_event` 129 行里有 4 行来自更早一次运行。后果是**看板直接 `SELECT COUNT(*)` 会得到过期数字**——表里 `overdue=1` 有 65 条，而本次产出的「承诺逾期」事件只有 52 条。

**不能用 DELETE 解决**，那正是 C1 要避免的。正确做法是标记批次、按最新批次过滤，陈旧行保留但不计入统计，主管标记也不丢。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_freshness.py
import pytest

from agent import l1, llm, pipeline, risk, rules
from etl import db, derive, loader


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    """写库的测试必须用隔离的临时库（M2 裁决 R7）。"""
    c = db.connect(tmp_path_factory.mktemp("db") / "t.db")
    db.create_tables(c)
    loader.load_raw_tables(c)
    derive.build_all(c)
    yield c
    c.close()


def _l1(sid, emotion=3):
    """用关键字参数构造，避免字段顺序变动时静默错位。"""
    return l1.L1Result(
        session_id=sid, scene_minor="催发货", scene_major="物流服务",
        confidence=0.9, emotion=emotion, summary="s", risk_tags=[],
        high_risk=False, promises=[], model="qwen3.8-flash",
        tokens_in=0, tokens_out=0, degraded=False,
    )


def test_last_batch_at_column_exists(conn):
    for table in ("promise", "risk_event"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        assert "last_batch_at" in cols, f"{table} 缺 last_batch_at 列"


def test_persist_stamps_batch_at(conn):
    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    risk.persist(conn, events, "2026-09-07T10:00:00Z")
    rows = conn.execute(
        "SELECT DISTINCT last_batch_at FROM risk_event").fetchall()
    assert [r["last_batch_at"] for r in rows] == ["2026-09-07T10:00:00Z"]
    conn.execute("DELETE FROM risk_event")
    conn.commit()


def test_stale_rows_keep_old_stamp_and_supervisor_state(conn):
    """陈旧行必须保留，且主管的处置标记不能丢——只是批次戳变旧。"""
    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    risk.persist(conn, events, "2026-09-06T10:00:00Z")

    row = conn.execute("SELECT * FROM risk_event LIMIT 1").fetchone()
    conn.execute(
        "UPDATE risk_event SET status='已闭环', handler='主管小李' WHERE id=?",
        (row["id"],))
    conn.commit()

    # 第二批只重算其中一部分事件
    subset = [e for e in events if e.session_id == row["session_id"]]
    risk.persist(conn, subset, "2026-09-07T10:00:00Z")

    kept = conn.execute("SELECT * FROM risk_event WHERE id=?",
                        (row["id"],)).fetchone()
    assert kept["status"] == "已闭环", "主管标记被洗掉了"
    assert kept["handler"] == "主管小李"
    assert kept["last_batch_at"] == "2026-09-07T10:00:00Z"

    stale = conn.execute(
        "SELECT COUNT(*) c FROM risk_event WHERE last_batch_at='2026-09-06T10:00:00Z'"
    ).fetchone()["c"]
    assert stale > 0, "陈旧行应当保留而不是被删除"
    conn.execute("DELETE FROM risk_event")
    conn.commit()


def test_current_batch_at_returns_latest(conn):
    conn.execute("DELETE FROM risk_event")
    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    risk.persist(conn, events, "2026-09-06T10:00:00Z")
    risk.persist(conn, events[:3], "2026-09-07T10:00:00Z")
    assert pipeline.current_batch_at(conn) == "2026-09-07T10:00:00Z"
    conn.execute("DELETE FROM risk_event")
    conn.commit()


def test_current_batch_at_none_when_empty(conn):
    conn.execute("DELETE FROM risk_event")
    conn.execute("DELETE FROM promise")
    conn.commit()
    assert pipeline.current_batch_at(conn) is None


def test_run_batch_stamps_both_tables(conn):
    class Stub:
        def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
            import json
            if model == "qwen3.8-flash":
                return llm.LLMResponse(json.dumps({
                    "scene_minor": "催发货", "confidence": 0.9, "emotion": 3,
                    "summary": "买家催发货", "high_risk": False, "risk_tags": [],
                    "promises": [{"text": "您的订单预计48小时内发出",
                                  "amount": 48, "unit": "hour"}],
                }, ensure_ascii=False), model, 100, 20)
            return llm.LLMResponse(json.dumps({
                "risk_attribution": "x", "suggested_actions": ["a"],
                "replies": [{"tone": "安抚", "text": "y"}],
            }, ensure_ascii=False), model, 200, 50)

    pipeline.run_batch(conn, Stub(), ["S00099"])
    stamp = pipeline.current_batch_at(conn)
    assert stamp is not None
    for table in ("promise", "risk_event"):
        n = conn.execute(
            f"SELECT COUNT(*) c FROM {table} WHERE last_batch_at=?",
            (stamp,)).fetchone()["c"]
        assert n > 0, f"{table} 没有被打上本批次戳"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_freshness.py -v`
Expected: FAIL — `promise 缺 last_batch_at 列`

- [ ] **Step 3: 加列**

在 `etl/db.py` 的 `DERIVED_COLUMNS` 里，给 `promise` 与 `risk_event` 各加一行：

```python
"last_batch_at": "TEXT",
```

`DERIVED_COLUMNS` 是派生表列的单一事实来源，`_migrate_columns` 会自动给已存在的旧库补列（M1 铺好的路，别绕开它另建表）。

- [ ] **Step 4: 改 `agent/risk.py` 的 persist**

签名加第三个参数，SQL 的插入列与 `DO UPDATE SET` 都加上 `last_batch_at`：

```python
def persist(conn: sqlite3.Connection, events: list[RiskEvent],
            batch_at: str) -> int:
    """幂等写入。upsert 而非 INSERT OR REPLACE——后者会删行，洗掉主管的
    status/handler 标记（M2 裁决 C1）。

    batch_at 标记本行属于哪一批批处理。陈旧行不删除，靠这个戳过滤。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO risk_event"
        " (risk_type, level, session_id, buyer, ticket_no, detected_by,"
        "  status, handler, detail, created_at, updated_at, last_batch_at)"
        " VALUES (?, ?, ?, ?, ?, ?, '待处理', NULL, ?, ?, ?, ?)"
        " ON CONFLICT(risk_type, session_id, detected_by) DO UPDATE SET"
        "   level = excluded.level,"
        "   ticket_no = excluded.ticket_no,"
        "   detail = excluded.detail,"
        "   updated_at = excluded.updated_at,"
        "   last_batch_at = excluded.last_batch_at",
        [(e.risk_type, e.level, e.session_id, e.buyer, e.ticket_no,
          e.detected_by, e.detail, now, now, batch_at) for e in events],
    )
    conn.commit()
    return len(events)
```

- [ ] **Step 5: 改 `agent/pipeline.py`**

加 `current_batch_at`，并在 `run_batch` 里生成批次戳、传给 `risk.persist`、写进 promise 的 INSERT：

```python
def current_batch_at(conn: sqlite3.Connection) -> str | None:
    """库内最新的批次戳。看板按它过滤，避免统计到陈旧行。"""
    stamps = []
    for table in ("promise", "risk_event"):
        row = conn.execute(
            f"SELECT MAX(last_batch_at) m FROM {table}").fetchone()
        if row is not None and row["m"]:
            stamps.append(row["m"])
    return max(stamps) if stamps else None
```

`run_batch` 开头生成 `batch_at = datetime.now(timezone.utc).isoformat(timespec="seconds")`，promise 的 `INSERT OR REPLACE` 语句加上 `last_batch_at` 列与对应的值，`risk.persist(conn, events, batch_at)`。

- [ ] **Step 6: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_freshness.py -v`
Expected: 6 passed

- [ ] **Step 7: 跑全量测试**

Run: `.venv/bin/python -m pytest -v`
Expected: 全绿

`risk.persist` 的签名变了，既有调用点共 **5 处**都要补第三个参数：
`agent/pipeline.py:228`，以及 `tests/test_risk.py` 的第 154 / 156 / 174 / 185 行。
测试里传一个固定戳（如 `"2026-09-07T00:00:00Z"`）即可。

- [ ] **Step 8: 提交**

```bash
git add etl/db.py agent/risk.py agent/pipeline.py tests/test_freshness.py
git commit -m "feat(agent): 批次戳标记，解决陈旧行累积"
```

---

### Task 2: 话术合规校验器

**Files:**
- Create: `agent/compliance.py`
- Test: `tests/test_compliance.py`

**Interfaces:**
- Consumes: `etl.db.connect`、`etl.schema.TICKET_TABLES`
- Produces:
  - `agent.compliance.ComplianceIssue` — dataclass：`kind: str`、`severity: str`、`detail: str`、`excerpt: str`
  - `agent.compliance.ComplianceReport` — dataclass：`session_id: str`、`tone: str`、`text: str`、`issues: list[ComplianceIssue]`，属性 `blocked: bool`（存在 `severity == "阻断"` 的问题时为 True）
  - `agent.compliance.KIND_FABRICATED_ID / KIND_HONORIFIC / KIND_NEW_PROMISE` — 三个 kind 常量
  - `agent.compliance.SEV_BLOCK / SEV_WARN / SEV_INFO` — 三个 severity 常量（`"阻断"` / `"警告"` / `"提示"`）
  - `agent.compliance.known_identifiers(conn) -> frozenset[str]` — 库内全部真实订单号/物流号/工单号，**335 个**（含工单表的 `orig_tracking_no` / `reissue_tracking_no`）
  - `agent.compliance.check_reply(conn, session_id: str, tone: str, text: str) -> ComplianceReport`
  - `agent.compliance.check_session(conn, session_id: str) -> list[ComplianceReport]` — 读 `session_summary.replies` 逐条校验

**这一层不调模型。** 确定性、可测、零成本是它的全部价值——也正因为确定，它的结论可以直接呈现给客服而不需要二次判断。

**三条规则与判级：**

| kind | severity | 规则 | 依据 |
|---|---|---|---|
| `fabricated_id` | **阻断** | 话术里出现的订单号/物流号/工单号必须能在库里查到 | spec §4.7「事实性内容一律由工具查出，模型碰都不碰」。编造单号是硬事实幻觉，客服直接发出去就是对买家撒谎。 |
| `invented_honorific` | 警告 | 出现「女士/先生/小姐」（买家昵称已脱敏，性别无从得知）或「主管/经理」（客服不是） | 不算撒谎但会尴尬；客服自己知道对方性别时可以改，所以是警告不是阻断。 |
| `new_promise` | 提示 | 话术里新增的时限承诺 | 不是错误——客服本来就该做承诺。但要**显式告诉他这条承诺会进入追踪**，这与本作品的承诺追踪功能形成闭环。 |

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_compliance.py
import json

import pytest

from agent import compliance
from etl import db


@pytest.fixture(scope="module")
def conn():
    """只读测试，可直连真实库。"""
    c = db.connect()
    yield c
    c.close()


def test_known_identifiers_covers_all_three_kinds(conn):
    ids = compliance.known_identifiers(conn)
    assert len(ids) == 335
    assert "6920947277927059788" in ids          # 订单号
    assert "YT7696503801519" in ids              # 物流号
    assert "KOC7263722" in ids                   # 工单号


def test_real_identifier_passes(conn):
    r = compliance.check_reply(
        conn, "S00099", "专业",
        "您的退货工单 KOC7263722 我这边正在盯办，有结果第一时间同步您。")
    assert [i for i in r.issues if i.kind == compliance.KIND_FABRICATED_ID] == []
    assert r.blocked is False


def test_fabricated_tracking_number_is_blocked(conn):
    """实测发现过：模型称「顺丰单号 SF12345678」，而真实单号是圆通 YT7667875838478。"""
    r = compliance.check_reply(
        conn, "S00006", "专业",
        "补发的洗发水已经打包完毕，顺丰单号是：SF1234567890，请注意查收。")
    ids = [i for i in r.issues if i.kind == compliance.KIND_FABRICATED_ID]
    assert len(ids) == 1
    assert "SF1234567890" in ids[0].excerpt
    assert ids[0].severity == compliance.SEV_BLOCK
    assert r.blocked is True


def test_fabricated_order_number_is_blocked(conn):
    r = compliance.check_reply(
        conn, "S00056", "专业", "我刚刚调取了订单 1234567890123456789 的财务后台数据。")
    assert r.blocked is True


def test_identifier_detected_when_adjacent_to_chinese(conn):
    """中文紧邻单号是最常见的形态，必须能检出。

    Python 的 \\b 在这里不成立（中文属于 \\w，「单」和「6」之间无边界），
    所以实现用的是前后向断言而不是 \\b。这条测试就是守这个的。
    """
    r = compliance.check_reply(
        conn, "S00056", "专业", "我刚刚调取了订单1234567890123456789的财务后台数据。")
    assert r.blocked is True, "中文紧邻的编造单号没被检出——检查是否误用了 \\b"


def test_short_numbers_are_not_flagged(conn):
    """手机号、金额这类短数字不该误报。"""
    r = compliance.check_reply(
        conn, "S00099", "专业", "订单金额1598元，如需联系请拨13812345678。")
    assert [i for i in r.issues if i.kind == compliance.KIND_FABRICATED_ID] == []


def test_invented_gender_honorific_is_warning(conn):
    """买家昵称是 邓e** 这样的脱敏形式，性别无从得知。"""
    r = compliance.check_reply(
        conn, "S00001", "致歉", "邓女士您好，非常抱歉让您久等了。")
    h = [i for i in r.issues if i.kind == compliance.KIND_HONORIFIC]
    assert len(h) == 1
    assert h[0].severity == compliance.SEV_WARN
    assert "女士" in h[0].excerpt
    assert r.blocked is False, "称谓问题不阻断，客服自己知道对方性别时可以改"


def test_invented_agent_title_is_warning(conn):
    r = compliance.check_reply(conn, "S00001", "致歉", "您好，我是客服主管，这边为您跟进。")
    h = [i for i in r.issues if i.kind == compliance.KIND_HONORIFIC]
    assert h and "主管" in h[0].excerpt


def test_new_promise_is_info_and_extracted(conn):
    r = compliance.check_reply(
        conn, "S00099", "专业", "您的订单我已加急标记，48小时内一定发出。")
    p = [i for i in r.issues if i.kind == compliance.KIND_NEW_PROMISE]
    assert len(p) >= 1
    assert p[0].severity == compliance.SEV_INFO
    assert "48小时内" in p[0].excerpt
    assert r.blocked is False


def test_clean_reply_has_no_issues(conn):
    r = compliance.check_reply(
        conn, "S00099", "安抚", "非常理解您着急的心情，我这边帮您盯着仓库进度。")
    assert r.issues == []
    assert r.blocked is False


def test_check_session_reads_replies_column(conn):
    reports = compliance.check_session(conn, "S00099")
    stored = json.loads(conn.execute(
        "SELECT replies FROM session_summary WHERE session_id='S00099'"
    ).fetchone()["replies"])
    assert len(reports) == len(stored)
    assert {r.tone for r in reports} == {x["tone"] for x in stored}


def test_check_session_empty_when_no_replies(conn):
    sid = conn.execute(
        "SELECT session_id FROM session_summary WHERE replies IN ('','[]') LIMIT 1"
    ).fetchone()
    if sid is None:
        pytest.skip("当前库内所有会话都有话术")
    assert compliance.check_session(conn, sid["session_id"]) == []


def test_known_identifiers_works_on_in_memory_db():
    """内存库上也必须能查出标识符——另开连接的实现会在这里静默返回空集合。"""
    import sqlite3 as _sq

    from etl import derive, loader

    mem = _sq.connect(":memory:")
    mem.row_factory = _sq.Row
    db.create_tables(mem)
    loader.load_raw_tables(mem)
    derive.build_all(mem)
    try:
        assert len(compliance.known_identifiers(mem)) == 335
    finally:
        mem.close()


def test_does_not_import_llm():
    """合规校验必须是确定性的，不得依赖模型。"""
    import inspect

    src = inspect.getsource(compliance)
    assert "agent.llm" not in src
    assert "openai" not in src
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_compliance.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.compliance'`

- [ ] **Step 3: 写实现**

```python
# agent/compliance.py
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_compliance.py -v`
Expected: 14 passed

> `_PROMISE` 正则里的 `(?:...)` 是非捕获组——`findall` 遇到捕获组会只返回组内容而不是整个匹配，那样 `excerpt` 就不对了。若 `test_new_promise_is_info_and_extracted` 断言 `"48小时内" in excerpt` 失败，先检查这一点。

- [ ] **Step 5: 提交**

```bash
git add agent/compliance.py tests/test_compliance.py
git commit -m "feat(agent): 话术合规校验器，确定性检测编造标识符/称谓/新承诺"
```

---

### Task 3: L2 提示词加固 + 合规统计入批处理报告

**Files:**
- Modify: `agent/l2.py`（`SYSTEM` 提示词）
- Modify: `agent/pipeline.py`（`BatchReport` 加合规字段、`format_report` 输出）
- Test: `tests/test_pipeline.py`（加合规统计断言）

**Interfaces:**
- Consumes: `agent.compliance.check_session`
- Produces:
  - `agent.pipeline.BatchReport` 新增字段：`replies_total: int`、`replies_blocked: int`、`replies_warned: int`、`replies_with_promise: int`
  - `format_report` 输出新增「话术合规」段

**提示词要加的约束**（写进 `agent/l2.py` 的 `SYSTEM`，措辞可润色但每条都要覆盖）：

```
生成话术时必须遵守（违反会被系统拦截）：
1. 禁止编造任何单号。订单号、物流单号、工单号只能引用上下文中已经出现过的，
   上下文里没有就不要提单号，改用「您的订单」「该工单」等指代。
2. 禁止使用「女士/先生/小姐」——买家昵称已脱敏，你无从得知其性别。
   直接说「您好」或「亲」。
3. 禁止自称「客服主管」「经理」等上下文未给出的身份。
4. 做时限承诺要克制：只在上下文已有依据时给出具体时限（如系统已显示预计
   发货时间），不要凭空承诺「今天内」「每 4 小时」这类无法核实的内容。
```

- [ ] **Step 1: 写失败的测试**

在 `tests/test_pipeline.py` 追加：

```python
def test_batch_report_counts_compliance(conn):
    """批处理报告要给出话术合规统计——这是本作品「AI 只建议不发送」铁律的量化。"""
    r = pipeline.run_batch(conn, RoutingClient(), ["S00005", "S00099"])
    assert r.replies_total > 0
    assert r.replies_blocked >= 0
    assert r.replies_warned >= 0
    assert r.replies_with_promise >= 0
    assert r.replies_blocked <= r.replies_total


def test_format_report_includes_compliance_section(conn):
    r = pipeline.run_batch(conn, RoutingClient(), ["S00099"])
    text = pipeline.format_report(r)
    assert "话术合规" in text
    assert "阻断" in text


def test_l2_system_prompt_forbids_fabrication():
    """加固条款必须在提示词里，否则模型没有约束。"""
    from agent import l2

    for must in ("禁止编造", "女士", "主管", "单号"):
        assert must in l2.SYSTEM, f"L2 提示词缺少约束: {must}"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -v`
Expected: FAIL — `AttributeError: 'BatchReport' object has no attribute 'replies_total'`

- [ ] **Step 3: 加固 L2 提示词**

把上面四条约束追加进 `agent/l2.py` 的 `SYSTEM` 字符串。**保留原有的三项输出要求与 JSON 格式说明不变**，只追加约束段。

- [ ] **Step 4: 批处理报告加合规统计**

`BatchReport` 加四个字段（默认 0）。`run_batch` 在写完 `session_summary` 之后、返回之前，对本批次有话术的会话跑一遍 `compliance.check_session`，累加统计。

`format_report` 在层级 token 表之后加一段：

```
话术合规：共 N 条 | 阻断 X（编造单号）| 警告 Y（编造称谓）| 含新承诺 Z
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/bin/python -m pytest -v`
Expected: 全绿

- [ ] **Step 6: 重录 L2 fixture**（提示词变了，旧 fixture 的 key 失效）

Run: `.venv/bin/python -m scripts.record_fixtures S00005 S00059 S00099 S00362`
删掉 key 已失效的旧 fixture 文件，避免留死文件。

- [ ] **Step 7: 提交**

```bash
git add agent/l2.py agent/pipeline.py tests/test_pipeline.py tests/fixtures/llm/
git commit -m "feat(agent): L2 提示词加固禁止编造事实，批处理报告加合规统计"
```

---

### Task 4: 重跑全量并出具加固前后对比

**Files:**
- 无代码改动（本任务只跑数据并记录结果）
- Create: `docs/m2-compliance-baseline.md`

**Interfaces:**
- Consumes: `agent.pipeline.run_batch`（CLI）、`agent.compliance.check_session`

**这一步产出的是 PPT 与评估要用的实证数字。** 加固前的基线已经测过（见本计划开头的表格），本任务测加固后的，两相对比。

- [ ] **Step 1: 先记录加固前基线**

在改动生效前，当前 `data/app.db` 里还是加固前的话术。跑一次统计并存档：

```bash
.venv/bin/python -c "
from agent import compliance
from etl import db
import collections
c = db.connect()
sids = [r['session_id'] for r in c.execute(\"SELECT session_id FROM session_summary WHERE replies NOT IN ('','[]')\")]
cnt = collections.Counter(); total = 0
for s in sids:
    for rep in compliance.check_session(c, s):
        total += 1
        for i in {x.kind for x in rep.issues}:
            cnt[i] += 1
print('加固前：话术', total, '条')
for k, n in cnt.most_common():
    print(f'  {k}: {n} 条 ({n*100//total}%)')
"
```

把输出贴进报告。

- [ ] **Step 2: 重跑真实全量**

Run: `.venv/bin/python -m agent.pipeline --price qwen3.8-flash:0.0003:0.0006 --price qwen3.7-plus:0.0008:0.002`

（单价是**占位值**，不是真实价目表，报告里必须注明。）

**前台跑，超时设 600000。** 不要放后台——本项目已有三次「以为启动了后台任务、实际没跑」的记录。

- [ ] **Step 3: 跑加固后统计**

用与 Step 1 完全相同的命令再跑一次，得到加固后的数字。

- [ ] **Step 4: 写对比文档**

创建 `docs/m2-compliance-baseline.md`，内容包含：

- 加固前后的三类问题占比对比表
- 仍被判「阻断」的话术清单（如果还有，逐条列出原文片段与编造的单号）
- 一句诚实结论：提示词加固能降低但**不能根除**幻觉，所以确定性校验器是必需的第二道防线——这正是把它做成阻断式校验而非仅靠提示词的理由

**如果加固后仍有阻断项，如实记录，不要反复调提示词直到归零。** 一个「提示词降到 X%、校验器兜住剩余」的诚实数据，比「我们调到 0 了」更可信，也更能说明双层防护的必要性。

- [ ] **Step 5: 核对与提交**

跑一次全量测试确认全绿，确认 pytest 前后三表计数一致（M2 裁决 R7 的测试隔离不能被破坏），然后提交。

```bash
git add docs/m2-compliance-baseline.md
git commit -m "docs: 话术合规加固前后实测对比"
```

---

## M3a 完成标准

- [ ] `promise` / `risk_event` 有 `last_batch_at` 列，`current_batch_at` 可用于过滤陈旧行
- [ ] 陈旧行保留但不计入最新批次；主管的 `status`/`handler` 标记在重跑后存活
- [ ] `agent/compliance.py` 三类检测可用，且**不 import 任何模型相关模块**
- [ ] 批处理报告输出「话术合规」段
- [ ] L2 提示词含四条加固约束
- [ ] 加固前后对比文档已产出，含真实数字
- [ ] `.venv/bin/python -m pytest` 全绿，且跑测试前后 `data/app.db` 三表计数不变

## 交给 M3b 的接口

| 接口 | 用途 |
|---|---|
| `compliance.check_session(conn, sid)` | 插件④卡片给每条话术标合规状态 |
| `ComplianceReport.blocked` | 决定「一键插入」按钮是否禁用 |
| `ComplianceIssue.severity` | 阻断/警告/提示三级视觉呈现 |
| `KIND_NEW_PROMISE` 的 issues | 插入前提示客服「这条承诺会进入追踪」，与承诺追踪功能闭环 |
| `pipeline.current_batch_at(conn)` | 看板按最新批次过滤，避免统计陈旧行 |
