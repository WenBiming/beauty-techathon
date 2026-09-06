# M2 Agent 三级路由 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建成 L0 规则 / L1 flash / L2 plus 三级成本分层的离线批处理流水线，跑通全部 138 个会话，产出会话画像、承诺追踪、风险事件与**实测成本对比表**。

**Architecture:** 一条离线流水线：L0 用纯 SQL 算确定性信号（零 token）→ L1 用 `qwen3.8-flash` 对全部会话做意图/情绪/摘要/承诺抽取 → L2 仅对高风险会话用 `qwen3.7-plus` 做风险归因与共情话术。模型层是可插拔适配器，测试用录制好的 fixture 回放，**测试永不联网**。

**Tech Stack:** Python 3.12 · openai SDK（DashScope OpenAI 兼容模式）· SQLite · pytest

**Spec:** `docs/superpowers/specs/2026-09-02-beauty-techathon-empathy-agent-design.md`（重点 §4 全节、§2.6 冒烟测试、§6.1 六类风险）

## Global Constraints

- Python 3.12；解释器 `/Users/wenbiming/dev/beauty-techathon/.venv/bin/python`
- **模型**：L1 = `qwen3.8-flash`，L2 = `qwen3.7-plus`，多模态 = `qwen3-vl-flash`。专属空间（eu-central-1）**没有** `qwen-turbo` / `qwen-vl`，不要用这些名字。
- **所有请求必须带 `extra_body={"enable_thinking": False}`**。这批是混合推理模型，开着思考链单会话 token 上升一个数量级，而本任务不需要长推理（spec §2.6）。
- **意图分类只预测 `scene_minor`（41 类），`scene_major` 由 `scene_map` 表反查**。41→10 是严格 1:1 映射，让模型猜 major 是纯粹的错误来源（spec §4.3）。
- **测试永不发起真实 API 调用**。全部走 `FixtureClient` 回放。
- **任何写库的测试必须用隔离的临时库**（`tmp_path_factory` + 全量 ETL，0.3 秒）。连真实 `data/app.db` 做 DELETE/UPDATE 会洗掉批处理产出，M3 依赖这些产出。只读测试可以直连真实库。
- 密钥从 `.env` 读，绝不入库、绝不打印
- 数据为虚构 MOCK；对外材料的声明文字不得删改
- YAGNI：不做向量库、不做模型微调、不做多 Agent 协作、不做流式输出、不做 LangChain 之类框架

## M1 已交付的接口（直接可用）

```python
# core/config.py
PROJECT_ROOT, DATA_DIR, DB_PATH, MOCK_IMAGE_DIR, ENV_PATH, SOURCE_XLSX  # Path
def load_env() -> None                      # 读 .env 到环境变量

# core/timeline.py
@dataclass(frozen=True)
class TimelineEvent:
    ts: str; kind: str; session_id: str | None; buyer: str
    title: str; detail: str; ref_id: str | None; is_open: bool
def buyer_timeline(conn, buyer: str) -> list[TimelineEvent]      # 跨会话，ts 升序
def session_timeline(conn, session_id: str) -> list[TimelineEvent]
# kind ∈ {"chat","order","ticket","promise"}；ts 保证可 datetime.fromisoformat 解析

# etl/db.py
def connect(path=None) -> sqlite3.Connection   # row_factory = sqlite3.Row
def create_tables(conn) -> None                # 含 schema 自动迁移
DERIVED_COLUMNS: dict[str, dict[str, str]]     # 派生表的单一事实来源，加列改这里

# etl/schema.py
TABLES: dict[str, TableSpec]; TICKET_TABLES: list[str]; CLOSED_STATUS = "已完结"

# etl/loader.py  load_raw_tables(conn) -> dict[str,int]
# etl/derive.py  build_all(conn) -> dict[str,int]
# etl/quality.py check(conn) -> QualityReport; format_report(r) -> str
# etl/build.py   build(db_path=None) -> QualityReport
```

**待写入的空表**（M1 已建好 schema）：

- `session_summary(session_id PK, summary, scene_major, scene_minor, intent_confidence, emotion, emotion_trend, risk_tags, suggested_actions, model, tokens_in, tokens_out, updated_at)`
- `risk_event(id, risk_type, level, session_id, buyer, ticket_no, detected_by, status, handler, detail, created_at, updated_at)` — 唯一索引 `(risk_type, session_id, detected_by)`
- `promise(id, message_id, session_id, buyer, promise_text, promise_type, made_at, deadline_at, ticket_no, closed, overdue)` — 唯一索引 `(message_id, promise_text)`
- `buyer_profile.risk_level` 列已预留，M1 写 NULL，本里程碑填

**M1 遗留的注意事项**（计划文档 §「其它交给 M2 的契约」）：

- `build_buyer_profile` 整表 DELETE 重建。若本里程碑给 `buyer_profile` 加列，必须加进 `DERIVED_COLUMNS` 并同步 `derive.py` 的 INSERT，否则会被清空。
- `ticket_reissue` **没有** `tracking_no` 列（拆成 `orig_tracking_no` / `reissue_tracking_no`）。写通用循环会 KeyError。
- `orders` 的时间列可能带中文后缀（如 `2026-04-30 09:53:30（定金）`，共 3 行）。`timeline` 已在展示层拆掉，**直接读原表的代码要自己处理**。

## 事实基线（测试断言引用这些实测数字）

| 项 | 值 |
|---|---|
| 会话 / 买家 / 消息 | 138 / 112 / 998 |
| 未完结工单 | 28（`status != '已完结'`） |
| 重复进线买家 | 24（22×2 次 + 2×3 次） |
| 含时限承诺的客服消息 | 184 条，覆盖 102/138 会话；硬 deadline 67 条 |
| L1 单会话 token（实测） | 均值 660（in 586 / out 74），延迟 1.44 秒 |
| 情景时钟下接入时已有未闭环工单的会话 | **仅 4 个**：S00045 / S00099 / S00102 / S00362 |
| 不良反应工单 | 10 张，4 张未完结 |

## File Structure

| 文件 | 职责 |
|---|---|
| `core/clock.py` | 双参考时钟（情景 / 全局） |
| `agent/__init__.py` | 包标记 |
| `agent/llm.py` | `LLMResponse` / `LLMClient` 协议 / DashScope / Fixture / Recording 三种实现 |
| `agent/rules.py` | L0 规则层，纯 SQL，零 token |
| `agent/retrieval.py` | 同场景历史成功话术检索（小 RAG，纯 SQL） |
| `agent/prompts.py` | L1 / L2 提示词模板，与代码分离便于迭代 |
| `agent/l1.py` | L1 会话分析：结构化输出 + 重试 + 降级 |
| `agent/promise.py` | 承诺 deadline 解析（含工作日）与闭环/逾期判定 |
| `agent/risk.py` | 六类风险事件生成，写 `risk_event` |
| `agent/l2.py` | L2 深度分析：风险归因 + 处置建议 + 共情话术 |
| `agent/tools.py` | function calling 工具集（6 个） |
| `agent/pipeline.py` | L0→L1→L2 编排、写库、成本报告、CLI |
| `scripts/record_fixtures.py` | 录制真实模型响应为 fixture（唯一联网的脚本） |
| `tests/fixtures/llm/` | 录制好的响应，测试回放用 |

---

### Task 1: 参考时钟

**Files:**
- Create: `core/clock.py`
- Test: `tests/test_clock.py`

**Interfaces:**
- Consumes: `etl.db.connect`
- Produces:
  - `core.clock.GLOBAL_NOW: str` — 数据集末尾时间字符串 `"2026-05-23 10:04:48"`
  - `core.clock.reference_now(conn, session_id: str | None = None) -> datetime` — 传 `session_id` 返回该会话最后一条消息时间（情景时钟）；不传返回全局时钟
  - `core.clock.add_business_days(start: datetime, n: int) -> datetime` — 往后推 n 个工作日，跳过周六周日

**背景**：数据集是 2026-05-05~05-23，系统跑在真实当下。实测若以真实今天为基准，28 张未完结工单 **28/28 都超 3 天**，预警失去区分度；以会话最后消息时间为基准则中位 1 天、最大 7 天，有区分度（spec §4.2.1）。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_clock.py
import datetime

import pytest

from core import clock
from etl import db


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    yield c
    c.close()


def test_global_now_is_dataset_end(conn):
    assert clock.GLOBAL_NOW == "2026-05-23 10:04:48"
    assert clock.reference_now(conn) == datetime.datetime(2026, 5, 23, 10, 4, 48)


def test_session_clock_is_last_message_time(conn):
    """S00099 是演示主样本，客服接入那一刻的时间。"""
    assert clock.reference_now(conn, "S00099") == datetime.datetime(2026, 5, 9, 11, 34, 5)


def test_session_clock_differs_from_global(conn):
    assert clock.reference_now(conn, "S00005") < clock.reference_now(conn)


def test_unknown_session_falls_back_to_global(conn):
    assert clock.reference_now(conn, "S99999") == clock.reference_now(conn)


def test_add_business_days_within_week():
    # 2026-05-05 是周二，+3 个工作日 = 05-08 周五
    tue = datetime.datetime(2026, 5, 5, 12, 0)
    assert clock.add_business_days(tue, 3) == datetime.datetime(2026, 5, 8, 12, 0)


def test_add_business_days_skips_weekend():
    # 2026-05-07 是周四，+2 个工作日跨过周末 = 05-11 周一
    thu = datetime.datetime(2026, 5, 7, 9, 0)
    assert clock.add_business_days(thu, 2) == datetime.datetime(2026, 5, 11, 9, 0)


def test_add_zero_business_days_is_identity():
    d = datetime.datetime(2026, 5, 5, 9, 0)
    assert clock.add_business_days(d, 0) == d
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_clock.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.clock'`

- [ ] **Step 3: 写实现**

```python
# core/clock.py
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_clock.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add core/clock.py tests/test_clock.py
git commit -m "feat(core): 双参考时钟（情景/全局）与工作日计算"
```

---

### Task 2: L0 规则层

**Files:**
- Create: `agent/__init__.py`
- Create: `agent/rules.py`
- Test: `tests/test_rules.py`

**Interfaces:**
- Consumes: `etl.db.connect`、`etl.schema.TICKET_TABLES`、`etl.schema.CLOSED_STATUS`、`core.clock.reference_now`
- Produces:
  - `agent.rules.L0Signals` — dataclass：`session_id: str`、`buyer: str`、`prior_session_count: int`、`hours_since_prior: float | None`、`open_ticket_count: int`、`max_ticket_age_days: int`、`has_adverse_reaction: bool`、`refund_ticket_count: int`、`max_response_gap_sec: int`、`turn_count: int`、`is_redline: bool`
  - `agent.rules.compute(conn, session_id: str) -> L0Signals`
  - `agent.rules.compute_all(conn) -> dict[str, L0Signals]`

**这一层零 token。** 这些信号是确定性可计算的，用 LLM 算既贵又不准（spec §4.2）。`is_redline` 为 True 时无条件触发 L2。

**关键事实（实测）：全库 80 张工单，80 张都建单于其所属会话的最后一条消息之后。** 所以要区分两种「工单可见性」：

- **`open_ticket_count` 只数「此前遗留」的未闭环工单**（其它会话的、且建单时间早于接入时点）。这是「信息孤岛」信号，全库只有 4 个会话命中——这正是它的价值所在，不要为了让数字好看去放宽它。
- **`has_adverse_reaction` 要包含本会话自己产生的不良反应工单**。本会话的工单是客服正在处理的事，他当然知道；若沿用「建单时间 <= 接入时点」的过滤，不良反应红线**永远不会触发**（10 张全部建单于会话之后）。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_rules.py
import pytest

from agent import rules
from etl import db


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    yield c
    c.close()


def test_compute_all_covers_every_session(conn):
    all_ = rules.compute_all(conn)
    assert len(all_) == 138


def test_demo_sample_third_contact(conn):
    """S00099：魏h** 第 3 次进线，接入时有 1 张未完结风控工单。"""
    s = rules.compute(conn, "S00099")
    assert s.buyer == "魏h**"
    assert s.prior_session_count == 2
    assert s.open_ticket_count == 1
    assert s.hours_since_prior is not None and s.hours_since_prior > 24


def test_first_contact_has_no_prior(conn):
    s = rules.compute(conn, "S00001")
    assert s.prior_session_count == 0
    assert s.hours_since_prior is None


def test_adverse_reaction_is_redline(conn):
    """S00082 的不良反应工单建单于会话之后，仍必须触发红线。"""
    s = rules.compute(conn, "S00082")
    assert s.has_adverse_reaction is True
    assert s.is_redline is True


def test_exactly_four_sessions_have_open_adverse_ticket(conn):
    """10 张不良反应工单里 4 张未完结，对应 4 个会话。"""
    hits = {k for k, v in rules.compute_all(conn).items() if v.has_adverse_reaction}
    assert hits == {"S00010", "S00058", "S00082", "S00268"}


def test_open_ticket_count_excludes_own_session_ticket(conn):
    """全库 80/80 张工单建单于其会话之后；open_ticket_count 只数此前遗留的。

    S00082 自己产生了一张未完结的不良反应工单，但它不算「此前遗留」。
    """
    s = rules.compute(conn, "S00082")
    assert s.has_adverse_reaction is True
    assert s.open_ticket_count == 0


def test_only_four_sessions_have_prior_open_ticket(conn):
    """情景时钟下仅 4 个会话在接入时已有未闭环工单（spec §4.2.1）。"""
    hits = {k for k, v in rules.compute_all(conn).items() if v.open_ticket_count > 0}
    assert hits == {"S00045", "S00099", "S00102", "S00362"}


def test_repeat_contact_sessions_count(conn):
    """24 个重复进线买家 → 26 个「非首次」会话（22×1 + 2×2）。"""
    n = sum(1 for v in rules.compute_all(conn).values() if v.prior_session_count > 0)
    assert n == 26


def test_response_gap_is_computed(conn):
    s = rules.compute(conn, "S00005")
    assert s.max_response_gap_sec > 0
    assert s.turn_count == 6
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_rules.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent'`

- [ ] **Step 3: 写实现**

```python
# agent/__init__.py
```

```python
# agent/rules.py
"""L0 规则层：零 token 的确定性信号。

这些信号有唯一正确答案，SQL 一定对而模型可能错，所以不交给大模型（spec §4.2）。
时间基准用情景时钟——客服接入那一刻真实看到的状态。
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from core.clock import reference_now
from etl.schema import CLOSED_STATUS, TICKET_TABLES

REDLINE_TICKET_AGE_DAYS = 3


@dataclass(frozen=True)
class L0Signals:
    session_id: str
    buyer: str
    prior_session_count: int
    hours_since_prior: float | None
    open_ticket_count: int
    max_ticket_age_days: int
    has_adverse_reaction: bool
    refund_ticket_count: int
    max_response_gap_sec: int
    turn_count: int
    is_redline: bool


def _session_rows(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT sent_at, role, buyer FROM chat WHERE session_id = ? ORDER BY sent_at",
        (session_id,),
    ).fetchall()


def compute(conn: sqlite3.Connection, session_id: str) -> L0Signals:
    rows = _session_rows(conn, session_id)
    if not rows:
        raise KeyError(f"未知会话 {session_id}")
    buyer = rows[0]["buyer"]
    now = reference_now(conn, session_id)
    start = datetime.fromisoformat(rows[0]["sent_at"])

    prior = conn.execute(
        "SELECT DISTINCT session_id, MAX(sent_at) AS t FROM chat"
        " WHERE buyer = ? AND sent_at < ? GROUP BY session_id ORDER BY t DESC",
        (buyer, rows[0]["sent_at"]),
    ).fetchall()
    hours_since_prior = None
    if prior:
        hours_since_prior = (
            start - datetime.fromisoformat(prior[0]["t"])
        ).total_seconds() / 3600

    open_count = 0
    max_age = 0
    adverse = False
    refunds = 0
    for table in TICKET_TABLES:
        for r in conn.execute(f"SELECT * FROM {table} WHERE buyer = ?", (buyer,)):
            created = datetime.fromisoformat(r["created_at"])
            own_session = r["session_id"] == session_id
            # 全库 80/80 张工单都建单于其会话结束之后。本会话自己的工单是客服
            # 正在处理的事，他当然知道；其它会话的工单只有建单早于接入时点才看得见。
            if not own_session and created > now:
                continue
            if table in ("ticket_payout", "ticket_return"):
                refunds += 1
            # 红线：本会话或历史遗留的未闭环不良反应工单
            if table == "ticket_adverse" and r["status"] != CLOSED_STATUS:
                adverse = True
            # 「信息孤岛」信号：只数此前遗留的未闭环工单，不含本会话自己产生的
            if r["status"] != CLOSED_STATUS and not own_session:
                open_count += 1
                max_age = max(max_age, (now - created).days)

    gaps = []
    for a, b in zip(rows, rows[1:]):
        if a["role"] == "买家" and b["role"] == "客服":
            gaps.append(
                int(
                    (
                        datetime.fromisoformat(b["sent_at"])
                        - datetime.fromisoformat(a["sent_at"])
                    ).total_seconds()
                )
            )

    redline = adverse or max_age > REDLINE_TICKET_AGE_DAYS

    return L0Signals(
        session_id=session_id,
        buyer=buyer,
        prior_session_count=len(prior),
        hours_since_prior=hours_since_prior,
        open_ticket_count=open_count,
        max_ticket_age_days=max_age,
        has_adverse_reaction=adverse,
        refund_ticket_count=refunds,
        max_response_gap_sec=max(gaps) if gaps else 0,
        turn_count=len(rows),
        is_redline=redline,
    )


def compute_all(conn: sqlite3.Connection) -> dict[str, L0Signals]:
    ids = [
        r["session_id"]
        for r in conn.execute("SELECT DISTINCT session_id FROM chat ORDER BY session_id")
    ]
    return {sid: compute(conn, sid) for sid in ids}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_rules.py -v`
Expected: 9 passed

> 这三条不良反应相关的断言来自实测：10 张工单中 4 张未完结（S00010 待处理 /
> S00058 处理中 / S00082 待处理 / S00268 处理中），且 **10 张全部建单于其会话
> 最后一条消息之后**。若断言失败，说明 `own_session` 那段过滤逻辑写错了——
> **不要改断言**。

- [ ] **Step 5: 提交**

```bash
git add agent/__init__.py agent/rules.py tests/test_rules.py
git commit -m "feat(agent): L0 规则层，零 token 确定性信号"
```

---

### Task 3: LLM 适配器（含 fixture 回放）

**Files:**
- Create: `agent/llm.py`
- Create: `tests/fixtures/llm/.gitkeep`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `core.config.load_env`
- Produces:
  - `agent.llm.LLMResponse` — dataclass：`text: str`、`model: str`、`tokens_in: int`、`tokens_out: int`
  - `agent.llm.fixture_key(model: str, system: str, user: str) -> str` — sha256 前 16 位
  - `agent.llm.LLMClient` — Protocol，方法 `complete(model, system, user, *, max_tokens=800, temperature=0.1) -> LLMResponse`
  - `agent.llm.FixtureClient(fixture_dir: Path)` — 回放；缺 fixture 抛 `FixtureMissing`
  - `agent.llm.DashScopeClient()` — 真实调用，**必带 `enable_thinking: False`**
  - `agent.llm.RecordingClient(inner, fixture_dir)` — 包一层真实客户端，把响应写成 fixture
  - `agent.llm.FixtureMissing` — 异常
  - `agent.llm.FIXTURE_DIR: Path` — `PROJECT_ROOT/tests/fixtures/llm`

**为什么要这层**：测试必须零联网、零成本、可重复。真实调用只发生在 `scripts/record_fixtures.py` 和最终的批处理运行。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_llm.py
import json

import pytest

from agent import llm


def test_fixture_key_is_stable_and_content_sensitive():
    a = llm.fixture_key("qwen3.8-flash", "sys", "user")
    b = llm.fixture_key("qwen3.8-flash", "sys", "user")
    c = llm.fixture_key("qwen3.8-flash", "sys", "USER")
    assert a == b and a != c
    assert len(a) == 16


def test_fixture_client_replays(tmp_path):
    key = llm.fixture_key("qwen3.8-flash", "S", "U")
    (tmp_path / f"{key}.json").write_text(
        json.dumps({"text": "hello", "model": "qwen3.8-flash",
                    "tokens_in": 10, "tokens_out": 3}),
        encoding="utf-8",
    )
    r = llm.FixtureClient(tmp_path).complete("qwen3.8-flash", "S", "U")
    assert r.text == "hello"
    assert r.tokens_in == 10 and r.tokens_out == 3


def test_fixture_client_raises_on_missing(tmp_path):
    with pytest.raises(llm.FixtureMissing) as e:
        llm.FixtureClient(tmp_path).complete("qwen3.8-flash", "S", "U")
    assert "record_fixtures" in str(e.value)


def test_recording_client_writes_fixture(tmp_path):
    class Fake:
        def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
            return llm.LLMResponse(text="X", model=model, tokens_in=1, tokens_out=2)

    rec = llm.RecordingClient(Fake(), tmp_path)
    rec.complete("qwen3.8-flash", "S", "U")
    key = llm.fixture_key("qwen3.8-flash", "S", "U")
    assert json.loads((tmp_path / f"{key}.json").read_text(encoding="utf-8"))["text"] == "X"
    # 写完之后 FixtureClient 能读回来
    assert llm.FixtureClient(tmp_path).complete("qwen3.8-flash", "S", "U").text == "X"


def test_dashscope_client_disables_thinking(monkeypatch):
    """混合推理模型开着思考链会让 token 上升一个数量级（spec §2.6）。"""
    captured = {}

    class FakeCompletions:
        def create(self, **kw):
            captured.update(kw)
            class U: prompt_tokens = 5; completion_tokens = 6
            class M: content = "ok"
            class C: message = M()
            class R: usage = U(); choices = [C()]
            return R()

    class FakeChat: completions = FakeCompletions()
    class FakeOpenAI:
        def __init__(self, **kw): self.chat = FakeChat()

    monkeypatch.setattr(llm, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    r = llm.DashScopeClient().complete("qwen3.8-flash", "S", "U")
    assert captured["extra_body"] == {"enable_thinking": False}
    assert captured["model"] == "qwen3.8-flash"
    assert r.tokens_in == 5 and r.tokens_out == 6
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_llm.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.llm'`

- [ ] **Step 3: 写实现**

```python
# agent/llm.py
"""模型适配器。

测试永不联网：全部走 FixtureClient 回放。真实调用只发生在
scripts/record_fixtures.py 和最终的批处理运行。

所有请求带 enable_thinking=False —— 这批是混合推理模型，开着思考链
单会话 token 上升一个数量级，而意图分类/情绪打分/摘要是理解与抽取
任务，不需要长推理（spec §2.6）。
"""
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from openai import OpenAI

from core.config import PROJECT_ROOT, load_env

FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "llm"


class FixtureMissing(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    tokens_in: int
    tokens_out: int


def fixture_key(model: str, system: str, user: str) -> str:
    h = hashlib.sha256()
    for part in (model, system, user):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


class LLMClient(Protocol):
    def complete(self, model: str, system: str, user: str, *,
                 max_tokens: int = 800, temperature: float = 0.1) -> LLMResponse: ...


class FixtureClient:
    """从磁盘回放录制好的响应。测试专用。"""

    def __init__(self, fixture_dir: Path | None = None) -> None:
        self.dir = Path(fixture_dir) if fixture_dir is not None else FIXTURE_DIR

    def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
        key = fixture_key(model, system, user)
        path = self.dir / f"{key}.json"
        if not path.is_file():
            raise FixtureMissing(
                f"缺少 fixture {path}。用 scripts/record_fixtures.py 录制后重试。"
            )
        return LLMResponse(**json.loads(path.read_text(encoding="utf-8")))


class DashScopeClient:
    """真实调用。阿里云百炼专属空间，OpenAI 兼容模式。"""

    def __init__(self) -> None:
        load_env()
        self._client = OpenAI(
            api_key=os.environ["DASHSCOPE_API_KEY"],
            base_url=os.environ["DASHSCOPE_BASE_URL"],
        )

    def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
        r = self._client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body={"enable_thinking": False},
        )
        return LLMResponse(
            text=(r.choices[0].message.content or "").strip(),
            model=model,
            tokens_in=r.usage.prompt_tokens,
            tokens_out=r.usage.completion_tokens,
        )


class RecordingClient:
    """包一层真实客户端，把响应落盘成 fixture。只在录制脚本里用。"""

    def __init__(self, inner: LLMClient, fixture_dir: Path | None = None) -> None:
        self.inner = inner
        self.dir = Path(fixture_dir) if fixture_dir is not None else FIXTURE_DIR

    def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
        r = self.inner.complete(model, system, user,
                                max_tokens=max_tokens, temperature=temperature)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{fixture_key(model, system, user)}.json").write_text(
            json.dumps(asdict(r), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return r
```

- [ ] **Step 4: 建 fixture 目录占位**

```bash
mkdir -p tests/fixtures/llm && touch tests/fixtures/llm/.gitkeep
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_llm.py -v`
Expected: 5 passed

- [ ] **Step 6: 提交**

```bash
git add agent/llm.py tests/test_llm.py tests/fixtures/llm/.gitkeep
git commit -m "feat(agent): LLM 适配器，DashScope/Fixture/Recording 三实现"
```

---

### Task 4: 相似案例检索（小 RAG）

**Files:**
- Create: `agent/retrieval.py`
- Test: `tests/test_retrieval.py`

**Interfaces:**
- Consumes: `etl.db.connect`
- Produces:
  - `agent.retrieval.SimilarCase` — dataclass：`session_id: str`、`scene_minor: str`、`buyer_message: str`、`agent_reply: str`
  - `agent.retrieval.search_similar_cases(conn, scene_minor: str, k: int = 3, exclude_session: str | None = None) -> list[SimilarCase]`

**为什么不用向量库**：检索条件是精确的结构化字段（41 类 `scene_minor` 之一），SQL 一个 WHERE 就够，还能顺带排序和排除测试集；138 个会话的规模上，embedding 调用与额外组件的成本远大于收益（spec §9 明确不做向量库）。

检索出的是**同场景下客服真实处理成功的话术**，作为 L2 生成共情话术的 few-shot——比凭空生成更贴业务口径，也比长 prompt 省 token（spec §4.4）。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_retrieval.py
import pytest

from agent import retrieval
from etl import db


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    yield c
    c.close()


def test_returns_cases_for_known_scene(conn):
    cases = retrieval.search_similar_cases(conn, "破损换货", k=3)
    assert 1 <= len(cases) <= 3
    for c in cases:
        assert c.scene_minor == "破损换货"
        assert c.buyer_message and c.agent_reply


def test_excludes_given_session(conn):
    cases = retrieval.search_similar_cases(conn, "破损换货", k=10,
                                           exclude_session="S00001")
    assert all(c.session_id != "S00001" for c in cases)


def test_unknown_scene_returns_empty(conn):
    assert retrieval.search_similar_cases(conn, "不存在的场景", k=3) == []


def test_respects_k(conn):
    assert len(retrieval.search_similar_cases(conn, "漏发赠品", k=2)) <= 2


def test_reply_follows_buyer_message(conn):
    """返回的必须是「买家问 → 客服答」的相邻配对，不是随机两条。"""
    for c in retrieval.search_similar_cases(conn, "泛红刺痒", k=3):
        rows = conn.execute(
            "SELECT role, message_text FROM chat WHERE session_id = ? ORDER BY sent_at",
            (c.session_id,),
        ).fetchall()
        texts = [r["message_text"] for r in rows]
        i = texts.index(c.buyer_message)
        assert rows[i]["role"] == "买家"
        assert rows[i + 1]["role"] == "客服"
        assert texts[i + 1] == c.agent_reply
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_retrieval.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.retrieval'`

- [ ] **Step 3: 写实现**

```python
# agent/retrieval.py
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
        for a, b in zip(rows, rows[1:]):
            if a["role"] == "买家" and b["role"] == "客服":
                out.append(SimilarCase(
                    session_id=sid, scene_minor=scene_minor,
                    buyer_message=a["message_text"], agent_reply=b["message_text"],
                ))
                break
    return out
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_retrieval.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add agent/retrieval.py tests/test_retrieval.py
git commit -m "feat(agent): 同场景成功话术检索，SQL 小 RAG"
```

---

### Task 5: 承诺 deadline 解析与逾期判定

**Files:**
- Create: `agent/promise.py`
- Test: `tests/test_promise.py`

**Interfaces:**
- Consumes: `core.clock.add_business_days`、`etl.schema.TICKET_TABLES`、`etl.schema.CLOSED_STATUS`
- Produces:
  - `agent.promise.RawPromise` — dataclass：`text: str`、`amount: int | None`、`unit: str | None`（`"hour"` / `"day"` / `"business_day"` / `None`）
  - `agent.promise.ResolvedPromise` — dataclass：`message_id: str`、`session_id: str`、`buyer: str`、`promise_text: str`、`promise_type: str`（`"hard"` / `"soft"`）、`made_at: str`、`deadline_at: str | None`、`ticket_no: str | None`、`closed: bool`、`overdue: bool`
  - `agent.promise.resolve_deadline(made_at: datetime, amount: int | None, unit: str | None) -> datetime | None`
  - `agent.promise.locate_message(conn, session_id: str, text: str) -> sqlite3.Row | None`
  - `agent.promise.evaluate(conn, session_id: str, raws: list[RawPromise], as_of: datetime) -> list[ResolvedPromise]`
  - `agent.promise.is_overdue_at(p: ResolvedPromise, as_of: datetime) -> bool`

**这一层是纯逻辑，不调模型。** L1 负责从话术里抽出 `(text, amount, unit)`，绝对时间的换算与闭环判定放在 Python 里——可测、可解释、零成本。

**关键设计：`overdue` 依赖「何时看」。** 承诺在会话中做出，deadline 通常在会话结束之后，所以用该会话自己的时钟判断永远不逾期。正确做法是传入 `as_of`：

- 存库时用**全局时钟**（看板视角：截至数据集末尾谁逾期了）
- 插件展示时用 `is_overdue_at(p, 情景时钟)` 现算（客服接入那一刻的视角）

实测依据：魏h\*\* 在 S00005（05-05 12:08）承诺「3 个工作日内」→ deadline 05-08；下一次进线 S00099 是 05-09，**那一刻已逾期**，而全库没有任何一张该买家的线下打款工单，即承诺从未兑现。

**软承诺（`amount` 为 None，如「马上帮您查」）只记录不预警**，否则误报会淹没真信号。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_promise.py
from datetime import datetime

import pytest

from agent import promise
from etl import db


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    yield c
    c.close()


def test_resolve_hours():
    made = datetime(2026, 5, 5, 12, 0)
    assert promise.resolve_deadline(made, 48, "hour") == datetime(2026, 5, 7, 12, 0)


def test_resolve_days():
    made = datetime(2026, 5, 5, 12, 0)
    assert promise.resolve_deadline(made, 2, "day") == datetime(2026, 5, 7, 12, 0)


def test_resolve_business_days_skips_weekend():
    """2026-05-05 是周二，+3 个工作日 = 05-08 周五。"""
    made = datetime(2026, 5, 5, 12, 0)
    assert promise.resolve_deadline(made, 3, "business_day") == datetime(2026, 5, 8, 12, 0)
    # 周四 +2 工作日 跨周末 = 下周一
    thu = datetime(2026, 5, 7, 9, 0)
    assert promise.resolve_deadline(thu, 2, "business_day") == datetime(2026, 5, 11, 9, 0)


def test_soft_promise_has_no_deadline():
    assert promise.resolve_deadline(datetime(2026, 5, 5), None, None) is None


def test_locate_message_finds_the_agent_turn(conn):
    row = promise.locate_message(conn, "S00005", "若3个工作日内仍未到账")
    assert row is not None
    assert row["role"] == "客服"
    assert row["sent_at"] == "2026-05-05 12:08:07"


def test_locate_message_returns_none_when_absent(conn):
    assert promise.locate_message(conn, "S00005", "这句话根本不存在于本会话") is None


def test_evaluate_marks_hard_promise_overdue_at_next_contact(conn):
    """魏h** 在 S00005 承诺 3 个工作日；到 05-09 再次进线时已逾期。"""
    raws = [promise.RawPromise("若3个工作日内仍未到账，我们走线下打款直接补给您",
                               3, "business_day")]
    out = promise.evaluate(conn, "S00005", raws, as_of=datetime(2026, 5, 9, 11, 34, 5))
    assert len(out) == 1
    p = out[0]
    assert p.promise_type == "hard"
    assert p.deadline_at == "2026-05-08 12:08:07"
    assert p.buyer == "魏h**"
    assert p.overdue is True
    assert p.closed is False


def test_evaluate_not_overdue_before_deadline(conn):
    raws = [promise.RawPromise("若3个工作日内仍未到账，我们走线下打款直接补给您",
                               3, "business_day")]
    out = promise.evaluate(conn, "S00005", raws, as_of=datetime(2026, 5, 6, 9, 0))
    assert out[0].overdue is False


def test_soft_promise_never_overdue(conn):
    raws = [promise.RawPromise("这边立刻为您核实", None, None)]
    out = promise.evaluate(conn, "S00005", raws, as_of=datetime(2026, 9, 5))
    assert out[0].promise_type == "soft"
    assert out[0].deadline_at is None
    assert out[0].overdue is False


def test_closed_when_session_ticket_finished(conn):
    """S00007 的补发换货工单 BH142092289687 已完结，其承诺应判为已闭环。"""
    raws = [promise.RawPromise("已为您创建补发工单，48小时内发出", 48, "hour")]
    out = promise.evaluate(conn, "S00007", raws, as_of=datetime(2026, 9, 5))
    assert out[0].closed is True
    assert out[0].overdue is False
    assert out[0].ticket_no == "BH142092289687"


def test_open_ticket_leaves_promise_unclosed(conn):
    """S00001 的补发换货工单 BH919209358357 仍是「进行中」，承诺不应判为闭环。"""
    raws = [promise.RawPromise("换货单已创建，48小时内发出", 48, "hour")]
    out = promise.evaluate(conn, "S00001", raws, as_of=datetime(2026, 9, 5))
    assert out[0].closed is False
    assert out[0].overdue is True
    assert out[0].ticket_no == "BH919209358357"


def test_is_overdue_at_is_pure(conn):
    raws = [promise.RawPromise("若3个工作日内仍未到账，我们走线下打款直接补给您",
                               3, "business_day")]
    p = promise.evaluate(conn, "S00005", raws, as_of=datetime(2026, 5, 6))[0]
    assert promise.is_overdue_at(p, datetime(2026, 5, 6)) is False
    assert promise.is_overdue_at(p, datetime(2026, 5, 9)) is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_promise.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.promise'`

- [ ] **Step 3: 写实现**

```python
# agent/promise.py
"""承诺 deadline 解析与逾期判定。纯逻辑，不调模型。

L1 只负责从客服话术里抽出 (原文, 数量, 单位)，绝对时间换算与闭环判定放在
这里——可测、可解释、零成本。

overdue 依赖「何时看」：承诺在会话中做出、deadline 通常在会话结束之后，
用该会话自己的时钟判断永远不逾期。所以 evaluate 要传 as_of：存库用全局
时钟（看板视角），插件展示用 is_overdue_at 按情景时钟现算。

软承诺（amount 为 None，如「马上帮您查」）只记录不预警，否则误报会淹没
真信号。
"""
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from core.clock import add_business_days
from etl.schema import CLOSED_STATUS, TICKET_TABLES

MATCH_PREFIX = 12          # locate_message 用前 N 字做子串匹配


@dataclass(frozen=True)
class RawPromise:
    text: str
    amount: int | None
    unit: str | None            # "hour" | "day" | "business_day" | None


@dataclass(frozen=True)
class ResolvedPromise:
    message_id: str
    session_id: str
    buyer: str
    promise_text: str
    promise_type: str           # "hard" | "soft"
    made_at: str
    deadline_at: str | None
    ticket_no: str | None
    closed: bool
    overdue: bool


def resolve_deadline(made_at: datetime, amount: int | None,
                     unit: str | None) -> datetime | None:
    if amount is None or unit is None:
        return None
    if unit == "hour":
        return made_at + timedelta(hours=amount)
    if unit == "day":
        return made_at + timedelta(days=amount)
    if unit == "business_day":
        return add_business_days(made_at, amount)
    raise ValueError(f"未知时间单位 {unit!r}，应为 hour/day/business_day/None")


def locate_message(conn: sqlite3.Connection, session_id: str,
                   text: str) -> sqlite3.Row | None:
    """找出承诺出自哪一条客服消息。用前 MATCH_PREFIX 字做子串匹配。"""
    needle = (text or "")[:MATCH_PREFIX]
    if not needle:
        return None
    for r in conn.execute(
        "SELECT * FROM chat WHERE session_id = ? AND role = '客服' ORDER BY sent_at",
        (session_id,),
    ):
        if needle in (r["message_text"] or ""):
            return r
    return None


def _session_ticket(conn: sqlite3.Connection,
                    session_id: str) -> tuple[str | None, bool]:
    """该会话关联的工单号与是否已完结。一个会话至多一张工单（spec §2.1）。"""
    for table in TICKET_TABLES:
        r = conn.execute(
            f"SELECT ticket_no, status FROM {table} WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if r is not None:
            return r["ticket_no"], r["status"] == CLOSED_STATUS
    return None, False


def is_overdue_at(p: ResolvedPromise, as_of: datetime) -> bool:
    """按给定时刻判断是否逾期。软承诺与已闭环承诺永不逾期。"""
    if p.promise_type == "soft" or p.deadline_at is None or p.closed:
        return False
    return datetime.fromisoformat(p.deadline_at) < as_of


def evaluate(conn: sqlite3.Connection, session_id: str,
             raws: list[RawPromise], as_of: datetime) -> list[ResolvedPromise]:
    fallback = conn.execute(
        "SELECT * FROM chat WHERE session_id = ? AND role = '客服'"
        " ORDER BY sent_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    ticket_no, ticket_closed = _session_ticket(conn, session_id)

    out: list[ResolvedPromise] = []
    for raw in raws:
        row = locate_message(conn, session_id, raw.text) or fallback
        if row is None:
            continue
        made = datetime.fromisoformat(row["sent_at"])
        deadline = resolve_deadline(made, raw.amount, raw.unit)
        p = ResolvedPromise(
            message_id=row["message_id"],
            session_id=session_id,
            buyer=row["buyer"],
            promise_text=raw.text,
            promise_type="hard" if deadline is not None else "soft",
            made_at=row["sent_at"],
            deadline_at=deadline.isoformat(sep=" ") if deadline else None,
            ticket_no=ticket_no,
            closed=ticket_closed,
            overdue=False,
        )
        out.append(replace(p, overdue=is_overdue_at(p, as_of)))
    return out
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_promise.py -v`
Expected: 12 passed

- [ ] **Step 5: 提交**

```bash
git add agent/promise.py tests/test_promise.py
git commit -m "feat(agent): 承诺 deadline 解析与逾期判定，含工作日换算"
```

---

### Task 6: L1 会话分析（提示词 + 结构化输出 + 降级）

**Files:**
- Create: `agent/prompts.py`
- Create: `agent/l1.py`
- Create: `scripts/record_fixtures.py`
- Test: `tests/test_l1.py`

**Interfaces:**
- Consumes: `agent.llm.LLMClient` / `LLMResponse`、`agent.promise.RawPromise`、`etl.db.connect`
- Produces:
  - `agent.prompts.L1_SYSTEM(scene_minors: list[str]) -> str`
  - `agent.prompts.render_dialogue(rows) -> str` — 把会话消息渲染成 `角色: 文本` 多行
  - `agent.l1.L1Result` — dataclass：`session_id`、`scene_minor: str`、`scene_major: str`、`confidence: float`、`emotion: int`、`summary: str`、`risk_tags: list[str]`、`promises: list[RawPromise]`、`model: str`、`tokens_in: int`、`tokens_out: int`、`degraded: bool`
  - `agent.l1.MODEL = "qwen3.8-flash"`
  - `agent.l1.analyse(conn, client, session_id: str, *, retries: int = 1) -> L1Result`
  - `agent.l1.parse_payload(conn, text: str) -> dict` — 剥 markdown 围栏 + JSON 解析 + 白名单校验，失败抛 `ValueError`

**四层可靠性防护**（spec §4.3）：JSON Schema 约束 → 剥 ```json 围栏 → 重试 1 次 → 二次失败降级为规则结果并标 `degraded=True`。批处理不能因为个别脏输出中断。

**只预测 `scene_minor`，`scene_major` 查 `scene_map` 表反查。** 模型返回的 `scene_minor` 不在 41 类白名单内即判为解析失败（spec §4.3）。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_l1.py
import json

import pytest

from agent import l1, llm, prompts
from etl import db


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    yield c
    c.close()


class StubClient:
    """按顺序吐出预设文本，记录收到的调用。"""

    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []

    def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
        self.calls.append((model, system, user))
        return llm.LLMResponse(text=self.texts.pop(0), model=model,
                               tokens_in=100, tokens_out=20)


GOOD = json.dumps({
    "scene_minor": "退款迟迟不到账", "confidence": 0.9, "emotion": 2,
    "summary": "买家反馈退款一周未到账", "risk_tags": ["退款时效投诉风险"],
    "promises": [{"text": "若3个工作日内仍未到账，我们走线下打款直接补给您",
                  "amount": 3, "unit": "business_day"}],
}, ensure_ascii=False)


def test_system_prompt_lists_all_41_scenes(conn):
    minors = [r["scene_minor"] for r in conn.execute(
        "SELECT scene_minor FROM scene_map ORDER BY scene_minor")]
    assert len(minors) == 41
    sys = prompts.L1_SYSTEM(minors)
    for m in minors:
        assert m in sys
    assert "scene_major" not in sys, "只让模型预测 minor，不要提 major"


def test_render_dialogue_includes_roles(conn):
    rows = conn.execute(
        "SELECT role, message_text FROM chat WHERE session_id='S00005' ORDER BY sent_at"
    ).fetchall()
    text = prompts.render_dialogue(rows)
    assert "买家:" in text and "客服:" in text
    assert "说好的原路退回呢" in text


def test_analyse_happy_path(conn):
    c = StubClient(GOOD)
    r = l1.analyse(conn, c, "S00005")
    assert r.scene_minor == "退款迟迟不到账"
    assert r.scene_major == "订单服务"          # 由 scene_map 反查，非模型输出
    assert r.emotion == 2
    assert r.degraded is False
    assert r.tokens_in == 100 and r.tokens_out == 20
    assert len(r.promises) == 1
    assert r.promises[0].unit == "business_day"
    assert c.calls[0][0] == "qwen3.8-flash"


def test_analyse_strips_markdown_fence(conn):
    r = l1.analyse(conn, StubClient(f"```json\n{GOOD}\n```"), "S00005")
    assert r.degraded is False
    assert r.scene_minor == "退款迟迟不到账"


def test_analyse_retries_once_then_succeeds(conn):
    c = StubClient("这不是 JSON", GOOD)
    r = l1.analyse(conn, c, "S00005")
    assert r.degraded is False
    assert len(c.calls) == 2


def test_analyse_degrades_after_second_failure(conn):
    c = StubClient("坏输出", "还是坏输出")
    r = l1.analyse(conn, c, "S00005")
    assert r.degraded is True
    assert r.scene_minor == ""
    assert r.emotion == 3                        # 降级为中性
    assert r.promises == []
    assert len(c.calls) == 2


def test_unknown_scene_minor_is_rejected(conn):
    bad = json.dumps({"scene_minor": "我编的场景", "confidence": 0.9, "emotion": 3,
                      "summary": "x", "risk_tags": [], "promises": []},
                     ensure_ascii=False)
    c = StubClient(bad, bad)
    r = l1.analyse(conn, c, "S00005")
    assert r.degraded is True, "白名单外的场景必须判为解析失败"


def test_emotion_out_of_range_is_rejected(conn):
    bad = json.dumps({"scene_minor": "退款迟迟不到账", "confidence": 0.9, "emotion": 9,
                      "summary": "x", "risk_tags": [], "promises": []},
                     ensure_ascii=False)
    r = l1.analyse(conn, StubClient(bad, bad), "S00005")
    assert r.degraded is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_l1.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.prompts'`

- [ ] **Step 3: 写 prompts.py**

```python
# agent/prompts.py
"""提示词模板，与代码分离便于迭代。"""

_L1_TEMPLATE = """你是美妆电商客服辅助系统的会话分析引擎。给定一段完整的客服-买家对话，输出结构化分析。

scene_minor 必须严格从以下列表中选一个，不得自创：
{scenes}

promises 是客服在本次对话中做出的时限承诺。对每条承诺：
- text 填客服原话
- 有明确时限的填 amount 与 unit，unit 取值只能是 hour / day / business_day
  例：「48小时内发出」-> amount=48, unit="hour"
      「1-3个工作日」 -> amount=3,  unit="business_day"（取上限，保守估计）
- 无明确时限的软承诺（如「马上帮您查」「加急处理」）填 amount=null, unit=null
- 没有任何承诺则返回空数组

emotion 取 1-5 的整数：1=极度不满 2=不满 3=中性 4=满意 5=非常满意

只输出 JSON，不要 markdown 代码块，不要任何解释。格式：
{{"scene_minor":"...","confidence":0.0到1.0,"emotion":1到5,"summary":"一句话不超过30字","risk_tags":["风险标签"],"promises":[{{"text":"...","amount":数字或null,"unit":"hour或day或business_day或null"}}]}}"""


def L1_SYSTEM(scene_minors: list[str]) -> str:
    return _L1_TEMPLATE.format(scenes="、".join(scene_minors))


def render_dialogue(rows) -> str:
    """把会话消息渲染成模型输入。rows 需含 role 与 message_text。"""
    return "\n".join(f"{r['role']}: {r['message_text']}" for r in rows)
```

- [ ] **Step 4: 写 l1.py**

```python
# agent/l1.py
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
            promises=_to_raw_promises(data.get("promises", [])),
            model=MODEL, tokens_in=tokens_in, tokens_out=tokens_out, degraded=False,
        )

    return L1Result(
        session_id=session_id, scene_minor="", scene_major="", confidence=0.0,
        emotion=3, summary=f"L1 解析失败降级：{last_error}", risk_tags=[],
        promises=[], model=MODEL, tokens_in=tokens_in, tokens_out=tokens_out,
        degraded=True,
    )
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_l1.py -v`
Expected: 8 passed

- [ ] **Step 6: 写录制脚本**

```python
# scripts/record_fixtures.py
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
```

- [ ] **Step 7: 实际录制 4 个演示会话的 fixture（会联网，花费约几分钱）**

Run: `.venv/bin/python -m scripts.record_fixtures S00005 S00059 S00099 S00362`
Expected: 打印 4 行分析结果 + token 合计；`tests/fixtures/llm/` 下出现 4 个 json 文件

把实际输出贴进报告——我要看模型在这 4 个会话上的真实判断。

- [ ] **Step 8: 跑全量测试并提交**

Run: `.venv/bin/python -m pytest -v`
Expected: 全绿

```bash
git add agent/prompts.py agent/l1.py scripts/record_fixtures.py tests/test_l1.py tests/fixtures/llm/
git commit -m "feat(agent): L1 会话分析，结构化输出四层防护 + fixture 录制"
```

---

### Task 7: 六类风险事件生成

**Files:**
- Create: `agent/risk.py`
- Test: `tests/test_risk.py`

**Interfaces:**
- Consumes: `agent.rules.L0Signals`、`agent.l1.L1Result`、`agent.promise.ResolvedPromise`、`etl.schema.TICKET_TABLES` / `CLOSED_STATUS`
- Produces:
  - `agent.risk.RiskEvent` — dataclass：`risk_type: str`、`level: str`（`"红"` / `"橙"`）、`session_id: str | None`、`buyer: str`、`ticket_no: str | None`、`detected_by: str`（`"L0"` / `"L1"`）、`detail: str`
  - `agent.risk.RISK_TYPES: list[str]` — 六类的固定顺序
  - `agent.risk.detect(conn, signals: dict[str, L0Signals], l1_results: dict[str, L1Result], promises: list[ResolvedPromise]) -> list[RiskEvent]`
  - `agent.risk.persist(conn, events: list[RiskEvent]) -> int` — `INSERT OR REPLACE` 写 `risk_event`，返回写入条数

赛题点名的「舆情投诉、重复进线、情绪升级、重复退款」四类全覆盖，承诺逾期与不良反应未闭环是从数据里挖出来的增量（spec §6.1）。`risk_event` 有唯一索引 `(risk_type, session_id, detected_by)`，所以同一会话同一类风险只留一条，重跑幂等。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_risk.py
from datetime import datetime

import pytest

from agent import l1, promise, risk, rules
from etl import db, derive, loader


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    """写库的测试必须用隔离的临时库。

    连真实 data/app.db 会让 DELETE/UPDATE 洗掉批处理产出——实测发生过：
    跑完全量后再跑一次 pytest，promise 从 181 行掉到 2 行、risk_event 清零。
    ETL 全量重建仅 0.3 秒，module 作用域下每个测试文件只付一次。
    """
    c = db.connect(tmp_path_factory.mktemp("db") / "t.db")
    db.create_tables(c)
    loader.load_raw_tables(c)
    derive.build_all(c)
    yield c
    c.close()


def _l1(session_id, emotion=3, minor="催发货", major="物流服务"):
    return l1.L1Result(session_id=session_id, scene_minor=minor, scene_major=major,
                       confidence=0.9, emotion=emotion, summary="s", risk_tags=[],
                       promises=[], model="qwen3.8-flash", tokens_in=0, tokens_out=0,
                       degraded=False)


def test_six_risk_types_declared():
    assert len(risk.RISK_TYPES) == 6
    assert "不良反应未闭环" in risk.RISK_TYPES
    assert "承诺逾期" in risk.RISK_TYPES


def test_adverse_reaction_is_red(conn):
    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    adverse = [e for e in events if e.risk_type == "不良反应未闭环"]
    assert adverse, "10 张不良反应工单里有 4 张未完结，必须产出预警"
    assert all(e.level == "红" for e in adverse)
    assert all(e.detected_by == "L0" for e in adverse)


def test_repeat_contact_detected(conn):
    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    repeat = {e.session_id for e in events if e.risk_type == "重复进线"}
    assert "S00099" in repeat
    assert len(repeat) == 26        # 22 个买家各 1 次 + 2 个买家各 2 次


def test_third_contact_is_red_second_is_orange(conn):
    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    by_sess = {e.session_id: e for e in events if e.risk_type == "重复进线"}
    assert by_sess["S00099"].level == "红"       # 第 3 次进线
    assert by_sess["S00059"].level == "橙"       # 第 2 次进线


def test_emotion_escalation_detected_by_l1(conn):
    sig = rules.compute_all(conn)
    l1r = {k: _l1(k) for k in sig}
    l1r["S00059"] = _l1("S00059", emotion=1)
    events = risk.detect(conn, sig, l1r, [])
    esc = [e for e in events if e.risk_type == "情绪升级"]
    assert any(e.session_id == "S00059" for e in esc)
    assert all(e.detected_by == "L1" for e in esc)


def test_overdue_promise_becomes_risk(conn):
    sig = rules.compute_all(conn)
    ps = promise.evaluate(
        conn, "S00005",
        [promise.RawPromise("若3个工作日内仍未到账，我们走线下打款直接补给您",
                            3, "business_day")],
        as_of=datetime(2026, 5, 23, 10, 4, 48),
    )
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, ps)
    over = [e for e in events if e.risk_type == "承诺逾期"]
    assert len(over) == 1
    assert over[0].session_id == "S00005"
    assert over[0].buyer == "魏h**"


def test_persist_is_idempotent(conn):
    sig = rules.compute_all(conn)
    events = risk.detect(conn, sig, {k: _l1(k) for k in sig}, [])
    first = risk.persist(conn, events)
    before = conn.execute("SELECT COUNT(*) c FROM risk_event").fetchone()["c"]
    risk.persist(conn, events)
    after = conn.execute("SELECT COUNT(*) c FROM risk_event").fetchone()["c"]
    assert first == len(events)
    assert before == after
    conn.execute("DELETE FROM risk_event")
    conn.commit()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_risk.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.risk'`

- [ ] **Step 3: 写实现**

```python
# agent/risk.py
"""六类风险事件生成（spec §6.1）。

赛题点名的四类（舆情投诉/重复进线/情绪升级/重复退款）全覆盖；承诺逾期与
不良反应未闭环是从数据里挖出来的增量。

risk_event 的唯一索引是 (risk_type, session_id, detected_by)，所以同一会话
同一类风险只留一条，重跑幂等。
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from etl.schema import CLOSED_STATUS

TICKET_AGE_ALERT_DAYS = 3
REFUND_ALERT_COUNT = 2

RISK_TYPES = [
    "不良反应未闭环", "承诺逾期", "重复进线",
    "工单积压超期", "情绪升级", "重复退款风控",
]


@dataclass(frozen=True)
class RiskEvent:
    risk_type: str
    level: str                  # "红" | "橙"
    session_id: str | None
    buyer: str
    ticket_no: str | None
    detected_by: str            # "L0" | "L1"
    detail: str


def detect(conn: sqlite3.Connection, signals: dict, l1_results: dict,
           promises: list) -> list[RiskEvent]:
    out: list[RiskEvent] = []

    # 1. 不良反应未闭环（红线，L0 直读工单表）
    for r in conn.execute(
        "SELECT ticket_no, session_id, buyer, symptom, status FROM ticket_adverse"
        " WHERE status != ?", (CLOSED_STATUS,)
    ):
        out.append(RiskEvent(
            risk_type="不良反应未闭环", level="红", session_id=r["session_id"],
            buyer=r["buyer"], ticket_no=r["ticket_no"], detected_by="L0",
            detail=f"{r['symptom']}（工单状态：{r['status']}）",
        ))

    # 2. 承诺逾期（L1 抽取 + L0 判定）
    for p in promises:
        if p.overdue:
            out.append(RiskEvent(
                risk_type="承诺逾期", level="橙", session_id=p.session_id,
                buyer=p.buyer, ticket_no=p.ticket_no, detected_by="L0",
                detail=f"承诺「{p.promise_text}」应于 {p.deadline_at} 前兑现，未闭环",
            ))

    for sid, s in signals.items():
        # 3. 重复进线
        if s.prior_session_count >= 1:
            out.append(RiskEvent(
                risk_type="重复进线",
                level="红" if s.prior_session_count >= 2 else "橙",
                session_id=sid, buyer=s.buyer, ticket_no=None, detected_by="L0",
                detail=f"该买家此前已进线 {s.prior_session_count} 次",
            ))
        # 4. 工单积压超期
        if s.max_ticket_age_days > TICKET_AGE_ALERT_DAYS:
            out.append(RiskEvent(
                risk_type="工单积压超期", level="橙", session_id=sid, buyer=s.buyer,
                ticket_no=None, detected_by="L0",
                detail=f"存在挂起 {s.max_ticket_age_days} 天的未完结工单",
            ))
        # 6. 重复退款 / 风控异常
        if s.refund_ticket_count >= REFUND_ALERT_COUNT:
            out.append(RiskEvent(
                risk_type="重复退款风控", level="橙", session_id=sid, buyer=s.buyer,
                ticket_no=None, detected_by="L0",
                detail=f"该买家累计 {s.refund_ticket_count} 张退款/退货类工单",
            ))
        # 5. 情绪升级（L1）
        r1 = l1_results.get(sid)
        if r1 is not None and not r1.degraded and r1.emotion <= 2:
            out.append(RiskEvent(
                risk_type="情绪升级",
                level="红" if r1.emotion == 1 else "橙",
                session_id=sid, buyer=s.buyer, ticket_no=None, detected_by="L1",
                detail=f"情绪评分 {r1.emotion}/5：{r1.summary}",
            ))
    return out


def persist(conn: sqlite3.Connection, events: list[RiskEvent]) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT OR REPLACE INTO risk_event"
        " (risk_type, level, session_id, buyer, ticket_no, detected_by,"
        "  status, handler, detail, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, '待处理', NULL, ?, ?, ?)",
        [(e.risk_type, e.level, e.session_id, e.buyer, e.ticket_no,
          e.detected_by, e.detail, now, now) for e in events],
    )
    conn.commit()
    return len(events)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_risk.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add agent/risk.py tests/test_risk.py
git commit -m "feat(agent): 六类风险事件生成与幂等落库"
```

---

### Task 8: Agent 工具集（function calling）

**Files:**
- Create: `agent/tools.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: `core.timeline`、`core.clock.reference_now`、`agent.retrieval.search_similar_cases`、`agent.promise.is_overdue_at`、`etl.schema.TICKET_TABLES` / `CLOSED_STATUS`
- Produces（全部返回 JSON 可序列化的 dict / list）：
  - `agent.tools.get_buyer_timeline(conn, buyer: str, limit: int = 20) -> list[dict]`
  - `agent.tools.get_order(conn, order_no: str) -> dict | None`
  - `agent.tools.list_open_tickets(conn, buyer: str, as_of: str | None = None) -> list[dict]`
  - `agent.tools.search_cases(conn, scene_minor: str, k: int = 3) -> list[dict]`
  - `agent.tools.draft_ticket(conn, session_id: str, ticket_type: str) -> dict`
  - `agent.tools.check_promises(conn, buyer: str, as_of: str | None = None) -> list[dict]`
  - `agent.tools.TOOL_SCHEMAS: list[dict]` — OpenAI function calling 格式，6 项

**注意 M1 的契约**：`ticket_reissue` **没有** `tracking_no` 列（拆成 `orig_tracking_no` / `reissue_tracking_no`），写通用循环会 KeyError。`draft_ticket` 必须按工单类型取字段。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_tools.py
import json

import pytest

from agent import tools
from etl import db, derive, loader


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    """写库的测试必须用隔离的临时库。

    连真实 data/app.db 会让 DELETE/UPDATE 洗掉批处理产出——实测发生过：
    跑完全量后再跑一次 pytest，promise 从 181 行掉到 2 行、risk_event 清零。
    ETL 全量重建仅 0.3 秒，module 作用域下每个测试文件只付一次。
    """
    c = db.connect(tmp_path_factory.mktemp("db") / "t.db")
    db.create_tables(c)
    loader.load_raw_tables(c)
    derive.build_all(c)
    yield c
    c.close()


def test_six_tool_schemas_declared():
    names = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
    assert names == {"get_buyer_timeline", "get_order", "list_open_tickets",
                     "search_cases", "draft_ticket", "check_promises"}
    for s in tools.TOOL_SCHEMAS:
        assert s["type"] == "function"
        assert s["function"]["description"]
        json.dumps(s)                       # 必须可序列化


def test_get_buyer_timeline_is_json_serialisable(conn):
    evs = tools.get_buyer_timeline(conn, "魏h**")
    assert evs and json.dumps(evs, ensure_ascii=False)
    assert {"ts", "kind", "title", "detail", "is_open"} <= set(evs[0])


def test_get_order_returns_row(conn):
    o = tools.get_order(conn, "6920223542160114724")
    assert o is not None
    assert o["buyer"] == "魏h**"
    assert o["province"] == "福建省"


def test_get_order_unknown_returns_none(conn):
    assert tools.get_order(conn, "不存在的订单号") is None


def test_list_open_tickets_respects_as_of(conn):
    """魏h** 的风控工单 05-07 18:15 创建；05-06 时点还看不到它。"""
    assert tools.list_open_tickets(conn, "魏h**", as_of="2026-05-06 00:00:00") == []
    later = tools.list_open_tickets(conn, "魏h**", as_of="2026-05-09 11:34:05")
    assert [t["ticket_no"] for t in later] == ["KOC7263722"]
    assert later[0]["age_days"] >= 1


def test_draft_ticket_prefills_fields(conn):
    d = tools.draft_ticket(conn, "S00001", "补发换货")
    assert d["会话ID"] == "S00001"
    assert d["买家昵称"] == "邓e**"
    assert d["关联订单号"] == "6920185815517983396"
    assert "工单类型" in d


def test_draft_ticket_reissue_has_no_generic_tracking_no(conn):
    """ticket_reissue 拆成 orig/reissue 两个物流号，不能写通用 tracking_no。"""
    d = tools.draft_ticket(conn, "S00001", "补发换货")
    assert "原订单物流单号" in d
    assert "tracking_no" not in d


def test_check_promises_reads_promise_table(conn):
    conn.execute(
        "INSERT OR REPLACE INTO promise (message_id, session_id, buyer,"
        " promise_text, promise_type, made_at, deadline_at, ticket_no,"
        " closed, overdue) VALUES ('m-test','S00005','魏h**','测试承诺','hard',"
        " '2026-05-05 12:08:07','2026-05-08 12:08:07',NULL,0,1)"
    )
    conn.commit()
    try:
        ps = tools.check_promises(conn, "魏h**", as_of="2026-05-09 11:34:05")
        assert any(p["promise_text"] == "测试承诺" and p["overdue"] for p in ps)
    finally:
        conn.execute("DELETE FROM promise WHERE message_id = 'm-test'")
        conn.commit()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.tools'`

- [ ] **Step 3: 写实现**

```python
# agent/tools.py
"""Agent 工具集（function calling，spec §4.7）。

全部返回 JSON 可序列化结构。事实性内容（订单号、金额、物流状态、工单号）
一律由这些工具从库里查出来，模型碰都不碰——这是幻觉控制的第一道防线。
"""
import sqlite3
from dataclasses import asdict
from datetime import datetime

from agent.retrieval import search_similar_cases
from core.clock import reference_now
from core.timeline import buyer_timeline
from etl.schema import CLOSED_STATUS, TICKET_TABLES

# 工单类型 -> (表名, 预填字段中文名列表)
TICKET_DRAFT_FIELDS = {
    "补发换货": ("ticket_reissue",
                 ["工单类型", "售后原因", "发出商品货号", "发出商品名称", "数量",
                  "原订单物流单号", "快递公司", "发货仓库", "客诉加急"]),
    "线下打款": ("ticket_payout",
                 ["打款类型", "退款问题类型", "退款金额(元)", "支付宝实名",
                  "支付宝账号", "相关物流单号"]),
    "物流": ("ticket_logistics",
             ["问题类型", "快递公司", "问题包裹物流单号", "发货仓", "处理方案"]),
    "不良反应": ("ticket_adverse",
                 ["类型", "年龄", "肤质", "使用商品", "产品批次号", "不适部位",
                  "症状描述", "用后多久出现", "是否停用", "是否就医"]),
    "售后退货": ("ticket_return",
                 ["包裹类型", "退货原因", "退货物流单号", "快递公司", "签收建议",
                  "是否异常"]),
}


def _now(conn: sqlite3.Connection, as_of: str | None) -> datetime:
    return datetime.fromisoformat(as_of) if as_of else reference_now(conn)


def get_buyer_timeline(conn: sqlite3.Connection, buyer: str,
                       limit: int = 20) -> list[dict]:
    return [asdict(e) for e in buyer_timeline(conn, buyer)[-limit:]]


def get_order(conn: sqlite3.Connection, order_no: str) -> dict | None:
    r = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
    return dict(r) if r is not None else None


def list_open_tickets(conn: sqlite3.Connection, buyer: str,
                      as_of: str | None = None) -> list[dict]:
    now = _now(conn, as_of)
    out = []
    for table in TICKET_TABLES:
        for r in conn.execute(f"SELECT * FROM {table} WHERE buyer = ?", (buyer,)):
            created = datetime.fromisoformat(r["created_at"])
            if created > now or r["status"] == CLOSED_STATUS:
                continue
            out.append({"ticket_no": r["ticket_no"], "table": table,
                        "session_id": r["session_id"], "status": r["status"],
                        "created_at": r["created_at"],
                        "age_days": (now - created).days})
    return sorted(out, key=lambda t: t["created_at"])


def search_cases(conn: sqlite3.Connection, scene_minor: str, k: int = 3) -> list[dict]:
    return [asdict(c) for c in search_similar_cases(conn, scene_minor, k=k)]


def draft_ticket(conn: sqlite3.Connection, session_id: str,
                 ticket_type: str) -> dict:
    if ticket_type not in TICKET_DRAFT_FIELDS:
        raise KeyError(f"未知工单类型 {ticket_type!r}，"
                       f"应为 {sorted(TICKET_DRAFT_FIELDS)} 之一")
    chat = conn.execute(
        "SELECT buyer, order_no, shop FROM chat WHERE session_id = ? LIMIT 1",
        (session_id,),
    ).fetchone()
    if chat is None:
        raise KeyError(f"未知会话 {session_id}")
    order = get_order(conn, chat["order_no"]) if chat["order_no"] else None

    draft = {"会话ID": session_id, "买家昵称": chat["buyer"], "店铺": chat["shop"],
             "关联订单号": chat["order_no"]}
    for field in TICKET_DRAFT_FIELDS[ticket_type][1]:
        draft[field] = None
    if order is not None:
        if "快递公司" in draft:
            draft["快递公司"] = order["carrier"]
        if "原订单物流单号" in draft:
            draft["原订单物流单号"] = order["tracking_no"]
        if "使用商品" in draft:
            draft["使用商品"] = order["item_name"]
        if "发出商品名称" in draft:
            draft["发出商品名称"] = order["item_name"]
        if "发出商品货号" in draft:
            draft["发出商品货号"] = order["sku"]
    return draft


def check_promises(conn: sqlite3.Connection, buyer: str,
                   as_of: str | None = None) -> list[dict]:
    now = _now(conn, as_of)
    out = []
    for r in conn.execute(
        "SELECT * FROM promise WHERE buyer = ? ORDER BY made_at", (buyer,)
    ):
        overdue = bool(
            r["promise_type"] == "hard" and not r["closed"] and r["deadline_at"]
            and datetime.fromisoformat(r["deadline_at"]) < now
        )
        out.append({"promise_text": r["promise_text"], "made_at": r["made_at"],
                    "deadline_at": r["deadline_at"], "closed": bool(r["closed"]),
                    "overdue": overdue, "ticket_no": r["ticket_no"]})
    return out


def _schema(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


_STR = {"type": "string"}
_INT = {"type": "integer"}

TOOL_SCHEMAS = [
    _schema("get_buyer_timeline", "拉取该买家跨会话的完整服务轨迹（聊天/订单/工单/承诺按时间归并）",
            {"buyer": _STR, "limit": _INT}, ["buyer"]),
    _schema("get_order", "按订单号查订单详情与状态",
            {"order_no": _STR}, ["order_no"]),
    _schema("list_open_tickets", "列出该买家在指定时点仍未完结的工单及挂起天数",
            {"buyer": _STR, "as_of": _STR}, ["buyer"]),
    _schema("search_cases", "检索同场景下客服处理成功的历史话术，作为参考",
            {"scene_minor": _STR, "k": _INT}, ["scene_minor"]),
    _schema("draft_ticket", "为当前会话生成预填好的工单字段，供客服确认后提交",
            {"session_id": _STR, "ticket_type": _STR}, ["session_id", "ticket_type"]),
    _schema("check_promises", "查询该买家收到过的客服承诺及其闭环/逾期状态",
            {"buyer": _STR, "as_of": _STR}, ["buyer"]),
]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_tools.py -v`
Expected: 8 passed

- [ ] **Step 5: 提交**

```bash
git add agent/tools.py tests/test_tools.py
git commit -m "feat(agent): function calling 工具集 6 项"
```

---

### Task 9: L2 深度分析

**Files:**
- Create: `agent/l2.py`
- Test: `tests/test_l2.py`

**Interfaces:**
- Consumes: `agent.llm.LLMClient`、`agent.rules.L0Signals`、`agent.l1.L1Result`、`agent.retrieval.search_similar_cases`、`core.timeline.buyer_timeline`
- Produces:
  - `agent.l2.MODEL = "qwen3.7-plus"`
  - `agent.l2.Reply` — dataclass：`tone: str`、`text: str`
  - `agent.l2.L2Result` — dataclass：`session_id`、`risk_attribution: str`、`suggested_actions: list[str]`、`replies: list[Reply]`、`model: str`、`tokens_in: int`、`tokens_out: int`、`degraded: bool`
  - `agent.l2.should_trigger(signals: L0Signals, l1: L1Result) -> bool`
  - `agent.l2.build_context(conn, session_id, signals, l1) -> str`
  - `agent.l2.analyse(conn, client, session_id, signals, l1, *, retries: int = 1) -> L2Result`

**只有 L0 红线、情绪 ≤2、有未闭环工单、或第 3 次进线才进 L2**（spec §4.4）。输入包含全轨迹时间线与**同场景历史成功话术**作为 few-shot——比凭空生成更贴业务口径，也比长 prompt 省 token。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_l2.py
import json

import pytest

from agent import l1, l2, llm, rules
from etl import db


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    yield c
    c.close()


class StubClient:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []

    def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
        self.calls.append((model, system, user))
        return llm.LLMResponse(text=self.texts.pop(0), model=model,
                               tokens_in=500, tokens_out=200)


def _l1(sid, emotion=3, minor="催发货", major="物流服务"):
    return l1.L1Result(session_id=sid, scene_minor=minor, scene_major=major,
                       confidence=0.9, emotion=emotion, summary="s", risk_tags=[],
                       promises=[], model="qwen3.8-flash", tokens_in=0, tokens_out=0,
                       degraded=False)


GOOD = json.dumps({
    "risk_attribution": "表面是催发货，真实风险在上一单未闭环的风控工单",
    "suggested_actions": ["先兑现 05-05 的线下打款承诺", "催单加急标记"],
    "replies": [{"tone": "安抚", "text": "上次退款的事我这边已经盯着了"},
                {"tone": "专业", "text": "您4号的订单我已加急标记"}],
}, ensure_ascii=False)


def test_redline_triggers(conn):
    s = rules.compute(conn, "S00082")            # 不良反应会话
    assert l2.should_trigger(s, _l1("S00082")) is True


def test_low_emotion_triggers(conn):
    s = rules.compute(conn, "S00002")            # 普通售前咨询
    assert l2.should_trigger(s, _l1("S00002", emotion=2)) is True


def test_open_ticket_triggers(conn):
    s = rules.compute(conn, "S00099")            # 接入时有未闭环工单
    assert l2.should_trigger(s, _l1("S00099")) is True


def test_calm_first_contact_does_not_trigger(conn):
    s = rules.compute(conn, "S00002")
    assert l2.should_trigger(s, _l1("S00002", emotion=4)) is False


def test_degraded_l1_still_triggers(conn):
    """L1 降级说明我们看不清这个会话，宁可多花钱也要看清。"""
    bad = l1.L1Result(session_id="S00002", scene_minor="", scene_major="",
                      confidence=0.0, emotion=3, summary="", risk_tags=[],
                      promises=[], model="qwen3.8-flash", tokens_in=0,
                      tokens_out=0, degraded=True)
    assert l2.should_trigger(rules.compute(conn, "S00002"), bad) is True


def test_context_includes_timeline_and_fewshot(conn):
    s = rules.compute(conn, "S00099")
    ctx = l2.build_context(conn, "S00099", s, _l1("S00099"))
    assert "魏h**" in ctx
    assert "历史成功话术" in ctx
    assert "KOC7263722" in ctx, "全轨迹里必须带上未闭环工单"


def test_analyse_happy_path(conn):
    s = rules.compute(conn, "S00099")
    c = StubClient(GOOD)
    r = l2.analyse(conn, c, "S00099", s, _l1("S00099"))
    assert r.degraded is False
    assert c.calls[0][0] == "qwen3.7-plus"
    assert len(r.replies) == 2
    assert r.replies[0].tone == "安抚"
    assert r.tokens_in == 500 and r.tokens_out == 200


def test_analyse_degrades_after_second_failure(conn):
    s = rules.compute(conn, "S00099")
    c = StubClient("坏", "还是坏")
    r = l2.analyse(conn, c, "S00099", s, _l1("S00099"))
    assert r.degraded is True
    assert r.replies == []
    assert len(c.calls) == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_l2.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.l2'`

- [ ] **Step 3: 写实现**

```python
# agent/l2.py
"""L2 深度分析：风险归因 + 处置建议 + 共情话术。

只对高风险会话触发（spec §4.4）。输入包含全轨迹时间线与同场景历史成功
话术作为 few-shot——比凭空生成更贴业务口径，也比长 prompt 省 token。

话术只是候选，永远由人工客服决定发不发（spec §5.1）。
"""
import json
import sqlite3
from dataclasses import dataclass

from agent.llm import LLMClient
from agent.retrieval import search_similar_cases
from agent.tools import list_open_tickets
from core.clock import reference_now
from core.timeline import buyer_timeline

MODEL = "qwen3.7-plus"
TIMELINE_TAIL = 15
FEWSHOT_K = 3

SYSTEM = """你是资深美妆电商客服主管，为一线客服提供决策辅助。

给定一个买家的完整服务轨迹与当前会话，输出三项：
1. risk_attribution：这个会话真正的风险在哪、根因是什么。**注意买家嘴上问的未必是真实风险所在**，请结合历史轨迹判断。
2. suggested_actions：客服现在该做的事，按优先级排序，每条一个动作，不超过 4 条。
3. replies：2-3 条候选话术，供客服选用后**插入输入框再由人工决定是否发送**。每条标注语气（安抚/专业/致歉之一）。话术要具体、有信息量、不要模板腔。

只输出 JSON，不要 markdown 代码块，不要解释。格式：
{"risk_attribution":"...","suggested_actions":["..."],"replies":[{"tone":"安抚","text":"..."}]}"""


@dataclass(frozen=True)
class Reply:
    tone: str
    text: str


@dataclass(frozen=True)
class L2Result:
    session_id: str
    risk_attribution: str
    suggested_actions: list[str]
    replies: list[Reply]
    model: str
    tokens_in: int
    tokens_out: int
    degraded: bool


def should_trigger(signals, l1_result) -> bool:
    """L0 红线 / 情绪低 / 有未闭环工单 / 第 3 次进线 / L1 降级。

    L1 降级也触发：那说明我们看不清这个会话，宁可多花钱也要看清。
    """
    return bool(
        signals.is_redline
        or signals.open_ticket_count > 0
        or signals.prior_session_count >= 2
        or l1_result.degraded
        or l1_result.emotion <= 2
    )


def build_context(conn: sqlite3.Connection, session_id: str, signals,
                  l1_result) -> str:
    events = buyer_timeline(conn, signals.buyer)[-TIMELINE_TAIL:]
    lines = [
        f"买家：{signals.buyer}",
        f"当前会话：{session_id}（该买家此前已进线 {signals.prior_session_count} 次）",
        f"未闭环工单：{signals.open_ticket_count} 张"
        f"，最长挂起 {signals.max_ticket_age_days} 天",
        f"L1 判定场景：{l1_result.scene_major}/{l1_result.scene_minor}"
        f"，情绪 {l1_result.emotion}/5",
        "",
        "【全轨迹（最近事件）】",
    ]
    for e in events:
        mark = " ⚠未闭环" if e.is_open else ""
        lines.append(f"{e.ts} [{e.kind}] {e.title}：{e.detail}{mark}")

    # 未闭环工单必须显式列出，不能指望它「碰巧」落在时间线尾部。
    # as_of 必须传情景时钟——build_context 是会话视角，不传会退化成全局时钟，
    # 与头部摘要的 max_ticket_age_days（情景时钟）产生矛盾数字（spec §4.2.1）。
    now = reference_now(conn, session_id)
    open_tickets = list_open_tickets(conn, signals.buyer,
                                     as_of=now.isoformat(sep=" "))
    if open_tickets:
        lines += ["", "【未闭环工单】"]
        for t in open_tickets:
            lines.append(f"{t['ticket_no']}（{t['table']}）状态 {t['status']}"
                         f"，已挂起 {t['age_days']} 天")

    cases = search_similar_cases(conn, l1_result.scene_minor, k=FEWSHOT_K,
                                 exclude_session=session_id)
    if cases:
        lines += ["", "【同场景历史成功话术（供参考语气与口径）】"]
        for c in cases:
            lines.append(f"买家：{c.buyer_message}")
            lines.append(f"客服：{c.agent_reply}")
    return "\n".join(lines)


def _parse(text: str) -> dict:
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1] if "\n" in body else body
        body = body.removeprefix("json").strip()
        if body.endswith("```"):
            body = body[: body.rindex("```")]
    data = json.loads(body.strip())
    if not isinstance(data.get("replies"), list):
        raise ValueError("replies 必须是数组")
    if not isinstance(data.get("suggested_actions"), list):
        raise ValueError("suggested_actions 必须是数组")
    return data


def analyse(conn: sqlite3.Connection, client: LLMClient, session_id: str,
            signals, l1_result, *, retries: int = 1) -> L2Result:
    user = build_context(conn, session_id, signals, l1_result)
    tokens_in = tokens_out = 0
    last_error = None
    for _ in range(retries + 1):
        r = client.complete(MODEL, SYSTEM, user, max_tokens=1200)
        tokens_in += r.tokens_in
        tokens_out += r.tokens_out
        try:
            data = _parse(r.text)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            continue
        return L2Result(
            session_id=session_id,
            risk_attribution=str(data.get("risk_attribution", "")).strip(),
            suggested_actions=[str(a) for a in data["suggested_actions"]],
            replies=[Reply(tone=str(x.get("tone", "")), text=str(x.get("text", "")))
                     for x in data["replies"] if x.get("text")],
            model=MODEL, tokens_in=tokens_in, tokens_out=tokens_out, degraded=False,
        )
    return L2Result(session_id=session_id,
                    risk_attribution=f"L2 解析失败降级：{last_error}",
                    suggested_actions=[], replies=[], model=MODEL,
                    tokens_in=tokens_in, tokens_out=tokens_out, degraded=True)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_l2.py -v`
Expected: 8 passed

- [ ] **Step 5: 提交**

```bash
git add agent/l2.py tests/test_l2.py
git commit -m "feat(agent): L2 深度分析，风险归因与共情话术生成"
```

---

### Task 10: 批处理流水线与成本报告

**Files:**
- Create: `agent/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: 前 9 个任务的全部产出
- Produces:
  - `agent.pipeline.LayerCost` — dataclass：`layer: str`、`sessions: int`、`calls: int`、`tokens_in: int`、`tokens_out: int`
  - `agent.pipeline.BatchReport` — dataclass：`total_sessions: int`、`l1_degraded: int`、`l2_triggered: int`、`l2_degraded: int`、`promise_count: int`、`hard_promise_count: int`、`overdue_promise_count: int`、`risk_event_count: int`、`layers: list[LayerCost]`
  - `agent.pipeline.run_batch(conn, client, session_ids: list[str] | None = None) -> BatchReport`
  - `agent.pipeline.format_report(report: BatchReport, prices: dict[str, tuple[float, float]] | None = None) -> str`
  - 命令行：`python -m agent.pipeline [--fixtures] [--sessions S1 S2 ...]`

**成本单价不预设。** spec §4.5 明确「单价按运行当日 DashScope 官方价目表计算，不预设数字」。`format_report` 默认只报 token；传入 `prices`（模型名 → `(输入元/千token, 输出元/千token)`）才计算金额，并给出「全量走 L2 模型」的对照。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_pipeline.py
import json

import pytest

from agent import llm, pipeline
from etl import db, derive, loader


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    """写库的测试必须用隔离的临时库。

    连真实 data/app.db 会让 DELETE/UPDATE 洗掉批处理产出——实测发生过：
    跑完全量后再跑一次 pytest，promise 从 181 行掉到 2 行、risk_event 清零。
    ETL 全量重建仅 0.3 秒，module 作用域下每个测试文件只付一次。
    """
    c = db.connect(tmp_path_factory.mktemp("db") / "t.db")
    db.create_tables(c)
    loader.load_raw_tables(c)
    derive.build_all(c)
    yield c
    c.close()


L1_OK = json.dumps({
    "scene_minor": "催发货", "confidence": 0.9, "emotion": 2, "summary": "买家催发货",
    "risk_tags": ["时效风险"],
    "promises": [{"text": "您的订单预计48小时内发出", "amount": 48, "unit": "hour"}],
}, ensure_ascii=False)

L2_OK = json.dumps({
    "risk_attribution": "真实风险在上一单未闭环",
    "suggested_actions": ["先兑现承诺"],
    "replies": [{"tone": "安抚", "text": "上次的事我盯着呢"}],
}, ensure_ascii=False)


class RoutingClient:
    """按模型名返回不同响应，并记录每层调用次数。"""

    def __init__(self):
        self.calls = {"qwen3.8-flash": 0, "qwen3.7-plus": 0}

    def complete(self, model, system, user, *, max_tokens=800, temperature=0.1):
        self.calls[model] += 1
        if model == "qwen3.8-flash":
            return llm.LLMResponse(L1_OK, model, 586, 74)
        return llm.LLMResponse(L2_OK, model, 1500, 300)


def test_run_batch_on_four_demo_sessions(conn):
    c = RoutingClient()
    ids = ["S00005", "S00059", "S00099", "S00362"]
    r = pipeline.run_batch(conn, c, ids)
    assert r.total_sessions == 4
    assert r.l1_degraded == 0
    assert c.calls["qwen3.8-flash"] == 4, "L1 必须全量过一遍"
    assert 0 < c.calls["qwen3.7-plus"] <= 4, "L2 只对高风险触发"
    assert r.l2_triggered == c.calls["qwen3.7-plus"]


def test_l1_layer_tokens_are_accounted(conn):
    r = pipeline.run_batch(conn, RoutingClient(), ["S00005", "S00099"])
    l1_layer = next(l for l in r.layers if l.layer == "L1")
    assert l1_layer.calls == 2
    assert l1_layer.tokens_in == 586 * 2
    assert l1_layer.tokens_out == 74 * 2


def test_l0_layer_is_zero_token(conn):
    r = pipeline.run_batch(conn, RoutingClient(), ["S00005"])
    l0 = next(l for l in r.layers if l.layer == "L0")
    assert l0.tokens_in == 0 and l0.tokens_out == 0
    assert l0.sessions == 1


def test_session_summary_written(conn):
    pipeline.run_batch(conn, RoutingClient(), ["S00099"])
    row = conn.execute(
        "SELECT * FROM session_summary WHERE session_id = 'S00099'").fetchone()
    assert row["scene_minor"] == "催发货"
    assert row["scene_major"] == "物流服务"      # 反查得来
    assert row["emotion"] == 2
    assert json.loads(row["risk_tags"]) == ["时效风险"]
    assert row["tokens_in"] > 0


def test_promises_persisted(conn):
    conn.execute("DELETE FROM promise")
    conn.commit()
    r = pipeline.run_batch(conn, RoutingClient(), ["S00099"])
    assert r.promise_count >= 1
    assert conn.execute("SELECT COUNT(*) c FROM promise").fetchone()["c"] >= 1


def test_run_batch_is_idempotent(conn):
    ids = ["S00005", "S00099"]
    pipeline.run_batch(conn, RoutingClient(), ids)
    n1 = conn.execute("SELECT COUNT(*) c FROM risk_event").fetchone()["c"]
    p1 = conn.execute("SELECT COUNT(*) c FROM promise").fetchone()["c"]
    pipeline.run_batch(conn, RoutingClient(), ids)
    assert conn.execute("SELECT COUNT(*) c FROM risk_event").fetchone()["c"] == n1
    assert conn.execute("SELECT COUNT(*) c FROM promise").fetchone()["c"] == p1


def test_buyer_risk_level_is_persisted(conn):
    """M1 预留的 buyer_profile.risk_level 必须被填上（红 > 橙 > NULL）。"""
    conn.execute("UPDATE buyer_profile SET risk_level = NULL")
    conn.commit()
    pipeline.run_batch(conn, RoutingClient(), ["S00099"])
    row = conn.execute(
        "SELECT risk_level FROM buyer_profile WHERE buyer = '魏h**'").fetchone()
    assert row["risk_level"] in ("红", "橙")


def test_format_report_without_prices_shows_tokens_only(conn):
    r = pipeline.run_batch(conn, RoutingClient(), ["S00005", "S00099"])
    text = pipeline.format_report(r)
    assert "L0" in text and "L1" in text and "L2" in text
    assert "token" in text
    assert "¥" not in text, "没给单价就不该出现金额"


def test_format_report_with_prices_shows_savings(conn):
    r = pipeline.run_batch(conn, RoutingClient(), ["S00005", "S00099"])
    text = pipeline.format_report(
        r, prices={"qwen3.8-flash": (0.0003, 0.0006),
                   "qwen3.7-plus": (0.0008, 0.002)})
    assert "¥" in text
    assert "对照" in text and "节省" in text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.pipeline'`

- [ ] **Step 3: 写实现**

```python
# agent/pipeline.py
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -v`
Expected: 9 passed

- [ ] **Step 5: 用 fixture 跑通 4 个演示会话**

Run: `.venv/bin/python -m agent.pipeline --fixtures --sessions S00005 S00059 S00099 S00362`
Expected: 打印批处理报告；L1 调用 4 次

> 若报 `FixtureMissing`，说明 L2 的 fixture 还没录。用
> `.venv/bin/python -m scripts.record_fixtures S00005 S00059 S00099 S00362` 只录了 L1。
> 此时**先跑真实全量**（下一步），或临时用 `--sessions` 挑不触发 L2 的会话验证。

- [ ] **Step 6: 跑真实全量批处理（会联网，138 会话约 9.1 万 token L1 + L2 部分）**

Run: `.venv/bin/python -m agent.pipeline`
Expected: 完整报告

把**实际输出完整贴进报告**，我需要这些真实数字：L1 降级数、L2 触发率、承诺总数/硬承诺/逾期数、风险事件数、各层 token。

- [ ] **Step 7: 跑全量测试并提交**

Run: `.venv/bin/python -m pytest -v`
Expected: 全绿

```bash
git add agent/pipeline.py tests/test_pipeline.py
git commit -m "feat(agent): L0/L1/L2 批处理流水线与成本报告"
```

---

### Task 11: 多模态图片分析（按需触发）

**Files:**
- Create: `agent/vision.py`
- Test: `tests/test_vision.py`

**Interfaces:**
- Consumes: `agent.llm.fixture_key` / `LLMResponse` / `FixtureMissing`、`core.config.DATA_DIR` / `load_env`
- Produces:
  - `agent.vision.MODEL = "qwen3-vl-flash"`
  - `agent.vision.DAMAGE_TYPES: list[str]` — 10 类语义标签（与 `image_path` 的目录名一一对应）
  - `agent.vision.VisionResult` — dataclass：`session_id`、`image_path`、`damage_type: str`、`description: str`、`suggested_ticket_type: str`、`model`、`tokens_in`、`tokens_out`、`degraded: bool`
  - `agent.vision.sessions_with_images(conn) -> list[tuple[str, str]]` — `(session_id, image_path)`，全库 29 条
  - `agent.vision.encode_image(path: Path) -> str` — 返回 `data:image/jpeg;base64,...`
  - `agent.vision.VisionClient` — Protocol，方法 `complete_vision(model, system, text, image_data_uri, *, max_tokens=500) -> LLMResponse`
  - `agent.vision.DashScopeVisionClient()` / `agent.vision.FixtureVisionClient(dir=None)`
  - `agent.vision.analyse_image(conn, client, session_id, image_path, *, retries=1) -> VisionResult`

**只有会话含图片消息才调用**（全库 29 条），不做无意义的全量图片分析（spec §4.6）。产出用于自动判定补发/换货类型并预填工单。

**fixture key 用 `image_path` 而不是 base64 内容**——图片重新生成后 base64 会变，但语义没变，用路径做 key 才稳定。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_vision.py
import json

import pytest

from agent import llm, vision
from core.config import DATA_DIR
from etl import db


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    yield c
    c.close()


class StubVision:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []

    def complete_vision(self, model, system, text, image_data_uri, *, max_tokens=500):
        self.calls.append((model, image_data_uri[:30]))
        return llm.LLMResponse(text=self.texts.pop(0), model=model,
                               tokens_in=800, tokens_out=60)


GOOD = json.dumps({"damage_type": "broken_pump",
                   "description": "粉底液泵头断裂，无法按压出液",
                   "suggested_ticket_type": "补发换货"}, ensure_ascii=False)


def test_ten_damage_types_match_image_dirs():
    assert len(vision.DAMAGE_TYPES) == 10
    assert "broken_pump" in vision.DAMAGE_TYPES
    assert "refund_screenshot" in vision.DAMAGE_TYPES


def test_sessions_with_images_finds_29(conn):
    pairs = vision.sessions_with_images(conn)
    assert len(pairs) == 29
    assert all(p.startswith("mock_images/") for _, p in pairs)


def test_encode_image_returns_data_uri():
    path = DATA_DIR / "mock_images" / "broken_pump" / "S00001_03.jpg"
    uri = vision.encode_image(path)
    assert uri.startswith("data:image/jpeg;base64,")
    assert len(uri) > 100


def test_encode_image_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        vision.encode_image(tmp_path / "nope.jpg")


def test_analyse_happy_path(conn):
    c = StubVision(GOOD)
    r = vision.analyse_image(conn, c, "S00001", "mock_images/broken_pump/S00001_03.jpg")
    assert r.degraded is False
    assert r.damage_type == "broken_pump"
    assert r.suggested_ticket_type == "补发换货"
    assert c.calls[0][0] == "qwen3-vl-flash"
    assert r.tokens_in == 800


def test_unknown_damage_type_is_rejected(conn):
    bad = json.dumps({"damage_type": "我编的类型", "description": "x",
                      "suggested_ticket_type": "补发换货"}, ensure_ascii=False)
    r = vision.analyse_image(conn, StubVision(bad, bad), "S00001",
                             "mock_images/broken_pump/S00001_03.jpg")
    assert r.degraded is True


def test_analyse_degrades_after_second_failure(conn):
    c = StubVision("坏", "还是坏")
    r = vision.analyse_image(conn, c, "S00001",
                             "mock_images/broken_pump/S00001_03.jpg")
    assert r.degraded is True
    assert r.damage_type == ""
    assert len(c.calls) == 2


def test_fixture_key_uses_path_not_bytes():
    """图片重新生成后 base64 会变，但语义没变——key 必须用路径。"""
    k1 = vision.vision_fixture_key("qwen3-vl-flash", "mock_images/a/b.jpg")
    k2 = vision.vision_fixture_key("qwen3-vl-flash", "mock_images/a/b.jpg")
    k3 = vision.vision_fixture_key("qwen3-vl-flash", "mock_images/a/c.jpg")
    assert k1 == k2 != k3
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_vision.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.vision'`

- [ ] **Step 3: 写实现**

```python
# agent/vision.py
"""多模态图片分析（spec §4.6）。

只有会话含图片消息才调用——全库 29 条，不做无意义的全量分析。产出用于
自动判定补发/换货类型并预填工单。

官方未提供图片文件，data/mock_images/ 下是 M1 生成的占位图，路径与
chat.image_path 逐字对齐。
"""
import base64
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from openai import OpenAI

from agent.llm import FIXTURE_DIR, FixtureMissing, LLMResponse
from core.config import DATA_DIR, load_env

MODEL = "qwen3-vl-flash"

# 与 image_path 的目录名一一对应（M1 实测 10 个语义目录）
DAMAGE_TYPES = [
    "broken_pump", "broken_parcel", "wrong_shade", "missing_item", "short_item",
    "refund_screenshot", "logistics_screenshot", "live_promise", "swatch",
    "consult_card",
]

# 语义类型 -> 建议工单类型
TICKET_HINT = {
    "broken_pump": "补发换货", "broken_parcel": "物流", "wrong_shade": "补发换货",
    "missing_item": "补发换货", "short_item": "补发换货",
    "refund_screenshot": "线下打款", "logistics_screenshot": "物流",
    "live_promise": "补发换货", "swatch": "售后退货", "consult_card": "售后退货",
}

SYSTEM = f"""你是美妆电商售后图片审核助手。看图判断买家上传的是哪一类凭证或问题。

damage_type 必须严格从以下列表选一个：{"、".join(DAMAGE_TYPES)}
suggested_ticket_type 从以下选一个：补发换货、线下打款、物流、不良反应、售后退货

只输出 JSON，不要 markdown 代码块，不要解释。格式：
{{"damage_type":"...","description":"不超过30字的客观描述","suggested_ticket_type":"..."}}"""


@dataclass(frozen=True)
class VisionResult:
    session_id: str
    image_path: str
    damage_type: str
    description: str
    suggested_ticket_type: str
    model: str
    tokens_in: int
    tokens_out: int
    degraded: bool


def vision_fixture_key(model: str, image_path: str) -> str:
    """用图片路径而非字节做 key——图片重新生成后字节会变，语义没变。"""
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    h.update(image_path.encode("utf-8"))
    return "vl-" + h.hexdigest()[:16]


def sessions_with_images(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(r["session_id"], r["image_path"]) for r in conn.execute(
        "SELECT session_id, image_path FROM chat WHERE image_path IS NOT NULL"
        " ORDER BY session_id, sent_at")]


def encode_image(path: Path) -> str:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"图片不存在: {p}")
    return "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode("ascii")


class VisionClient(Protocol):
    def complete_vision(self, model: str, system: str, text: str,
                        image_data_uri: str, *, max_tokens: int = 500
                        ) -> LLMResponse: ...


class DashScopeVisionClient:
    def __init__(self) -> None:
        load_env()
        self._client = OpenAI(api_key=os.environ["DASHSCOPE_API_KEY"],
                              base_url=os.environ["DASHSCOPE_BASE_URL"])

    def complete_vision(self, model, system, text, image_data_uri, *, max_tokens=500):
        r = self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": image_data_uri}},
                ]},
            ],
            temperature=0.1, max_tokens=max_tokens,
            extra_body={"enable_thinking": False},
        )
        return LLMResponse(text=(r.choices[0].message.content or "").strip(),
                           model=model, tokens_in=r.usage.prompt_tokens,
                           tokens_out=r.usage.completion_tokens)


class FixtureVisionClient:
    def __init__(self, fixture_dir: Path | None = None) -> None:
        self.dir = Path(fixture_dir) if fixture_dir is not None else FIXTURE_DIR
        self._path: str | None = None

    def for_image(self, image_path: str) -> "FixtureVisionClient":
        self._path = image_path
        return self

    def complete_vision(self, model, system, text, image_data_uri, *, max_tokens=500):
        if self._path is None:
            raise FixtureMissing("调用前请先 for_image(image_path) 指定图片路径")
        f = self.dir / f"{vision_fixture_key(model, self._path)}.json"
        if not f.is_file():
            raise FixtureMissing(f"缺少 fixture {f}，用 scripts/record_fixtures.py 录制")
        return LLMResponse(**json.loads(f.read_text(encoding="utf-8")))


def _parse(text: str) -> dict:
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1] if "\n" in body else body
        body = body.removeprefix("json").strip()
        if body.endswith("```"):
            body = body[: body.rindex("```")]
    data = json.loads(body.strip())
    if data.get("damage_type") not in DAMAGE_TYPES:
        raise ValueError(f"damage_type 不在白名单内: {data.get('damage_type')!r}")
    return data


def analyse_image(conn: sqlite3.Connection, client: VisionClient, session_id: str,
                  image_path: str, *, retries: int = 1) -> VisionResult:
    uri = encode_image(DATA_DIR / image_path)
    tokens_in = tokens_out = 0
    last_error = None
    for _ in range(retries + 1):
        r = client.complete_vision(MODEL, SYSTEM, "请判断这张图属于哪一类。", uri)
        tokens_in += r.tokens_in
        tokens_out += r.tokens_out
        try:
            data = _parse(r.text)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            continue
        dtype = data["damage_type"]
        return VisionResult(
            session_id=session_id, image_path=image_path, damage_type=dtype,
            description=str(data.get("description", "")).strip(),
            suggested_ticket_type=str(
                data.get("suggested_ticket_type") or TICKET_HINT[dtype]),
            model=MODEL, tokens_in=tokens_in, tokens_out=tokens_out, degraded=False)

    return VisionResult(session_id=session_id, image_path=image_path, damage_type="",
                        description=f"VL 解析失败降级：{last_error}",
                        suggested_ticket_type="", model=MODEL, tokens_in=tokens_in,
                        tokens_out=tokens_out, degraded=True)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_vision.py -v`
Expected: 8 passed

- [ ] **Step 5: 用真实模型验一张图（会联网，一次调用）**

Run:
```bash
.venv/bin/python -c "
from agent import vision
from etl import db
c = db.connect()
r = vision.analyse_image(c, vision.DashScopeVisionClient(), 'S00001',
                         'mock_images/broken_pump/S00001_03.jpg')
print(r)
"
```

**预期结果需要如实记录**：这些是文字卡片占位图而非真实照片，模型很可能识别不出「泵头破损」。**这不是 bug**——把真实表现写进报告，M4 的评估会如实呈现多模态在占位图上的局限。不要为了让它「看起来能用」去改白名单或放宽校验。

- [ ] **Step 6: 跑全量测试并提交**

Run: `.venv/bin/python -m pytest -v`
Expected: 全绿

```bash
git add agent/vision.py tests/test_vision.py
git commit -m "feat(agent): 多模态图片分析，按需触发 qwen3-vl-flash"
```

---

## M2 完成标准

- [ ] `.venv/bin/python -m agent.pipeline` 跑通全部 138 个会话
- [ ] `session_summary` 138 行，`degraded` 会话数记录在案
- [ ] `promise` 表有数据，硬承诺与逾期数可查
- [ ] `risk_event` 表覆盖六类风险，重跑幂等
- [ ] 成本报告给出各层 token 实测值与「全量走 L2 模型」的对照
- [ ] `.venv/bin/python -m pytest` 全绿，且**测试全程不联网**
- [ ] `buyer_profile.risk_level` 被填充（M1 预留的列）
- [ ] 多模态链路可跑通，且**如实记录**占位图上的真实识别表现

> **注意**：若给 `buyer_profile` 加列或改列，必须同步 `etl/db.py` 的 `DERIVED_COLUMNS` 与
> `etl/derive.py` 的 INSERT——该表是整表 DELETE 重建的，漏改会被静默清空（M1 契约）。

## 交给 M3 的接口

| 接口 | 用途 |
|---|---|
| `session_summary` 表 | 插件秒开的数据源（缓存命中 = 0 token） |
| `risk_event` 表 | 看板事件流与插件风险徽章，两处共用同一份 |
| `promise` 表 + `promise.is_overdue_at` | 插件按情景时钟现算逾期，看板用存库值 |
| `agent.tools.*` + `TOOL_SCHEMAS` | 在线 Agent 的 function calling |
| `agent.l2.analyse` | 客服点「生成话术」时的实时调用 |
| `core.clock.reference_now(conn, session_id)` | 插件传 session_id，看板不传 |
| `agent.rules.compute(conn, session_id)` | 插件头卡的徽章数据 |
