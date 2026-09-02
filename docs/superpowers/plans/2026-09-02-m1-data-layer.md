# M1 数据层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把官方 Excel 的 7 张业务表变成一个可查询的 SQLite 数据层，并产出「跨会话全轨迹事件流」这一核心资产。

**Architecture:** 一次性幂等 ETL：Excel → 中英列名映射 → SQLite 原表（1:1，不清洗）→ 派生表（买家画像、场景映射）→ 数据质量报告。之上是一个只读查询层 `core/timeline.py`，把聊天/订单/工单三类记录按时间归并成统一事件流，供 M3 的插件与看板共用。

**Tech Stack:** Python 3.12 · uv · SQLite（stdlib `sqlite3`）· openpyxl · Pillow（生成 mock 图）· pytest

**Spec:** `docs/superpowers/specs/2026-09-02-beauty-techathon-empathy-agent-design.md`

## Global Constraints

- Python 3.12；依赖管理用 `uv`，虚拟环境在 `.venv/`
- 存储为 SQLite 单文件 `data/app.db`（零部署，可随源码提交，断网可运行）
- 原表 **1:1 映射 Excel，不做清洗**，保留原值便于溯源与举证（spec §3.2）
- ETL 必须**幂等**：重复执行结果一致（spec §3.5）
- `买家昵称` 是数据中**唯一的跨会话身份键**，无 buyer_id（spec §2.5.3）
- 数据为虚构 MOCK，官方已声明；所有对外材料须保留该声明（spec §2.5.4）
- 密钥绝不入库：`.env`、`*apiKey*.csv` 已在 `.gitignore`
- YAGNI（spec §9）：不做登录/权限、不做向量数据库、不做实时推送、不做移动端适配、不做多租户
- 官方数据源路径：`/Users/wenbiming/Documents/misc/AI-Assist/赛题 1：数据共情者-业务数据.xlsx`

## 事实基线（所有测试断言都引用这些实测数字）

| 项 | 值 |
|---|---|
| 聊天消息 / 会话 / 买家 | 998 / 138 / 112 |
| 订单 | 113 |
| 工单：补发换货 / 线下打款 / 物流 / 不良反应 / 售后退货 | 24 / 13 / 15 / 10 / 18 |
| 未完结工单合计（`status != '已完结'`） | 28（4+7+5+4+8） |
| `scene_major` / `scene_minor` 类数 | 10 / 41，**严格 1:1 映射** |
| 无订单且无工单的会话 | 25 |
| `image_path` 去重条数 / 语义目录数 | 29 / 10 |
| 演示金样本 魏h\*\* | 3 会话（S00005/S00059/S00099）、21 条聊天、3 订单、实付合计 556 元、1 张未完结工单 `KOC7263722` |

**状态列名不统一**：补发换货/线下打款/物流工单用 `工单状态`，不良反应/售后退货工单用 `任务状态`。ETL 统一映射为 `status`。

## File Structure

| 文件 | 职责 |
|---|---|
| `pyproject.toml` | 依赖与 pytest 配置 |
| `core/__init__.py` | 包标记 |
| `core/config.py` | 路径常量、`.env` 加载 |
| `core/timeline.py` | 只读查询层：统一事件流（M3 的插件/看板共用） |
| `etl/__init__.py` | 包标记 |
| `etl/schema.py` | 7 张原表的中英列名映射与 DDL |
| `etl/reader.py` | Excel → `list[dict]`（英文键） |
| `etl/db.py` | SQLite 连接与建表 |
| `etl/loader.py` | 灌数（幂等） |
| `etl/derive.py` | 派生表：`buyer_profile`、`scene_map` |
| `etl/quality.py` | 数据质量报告（含昵称碰撞检查） |
| `etl/build.py` | CLI 入口，串起全流程 |
| `scripts/gen_mock_images.py` | 生成 29 张 mock 图片，路径对齐 `image_path` |
| `tests/` | 对应测试 |

> 注：spec §8.2 的目录清单未列 `core/`。timeline 是被 app 与 agent 共同消费的**查询层**而非 ETL 步骤，放进 `etl/` 会造成错误的依赖方向，故新增 `core/` 包。这是对 spec §8.2 的补充，不改变任何设计决策。

---

### Task 1: 项目骨架与配置

**Files:**
- Create: `pyproject.toml`
- Create: `core/__init__.py`
- Create: `core/config.py`
- Create: `tests/__init__.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces: `core.config.PROJECT_ROOT: Path`、`core.config.DATA_DIR: Path`、`core.config.DB_PATH: Path`、`core.config.SOURCE_XLSX: Path`、`core.config.MOCK_IMAGE_DIR: Path`、`core.config.load_env() -> None`

- [ ] **Step 1: 写 pyproject.toml**

```toml
[project]
name = "xinji"
version = "0.1.0"
description = "心迹 EmpathyTrace — 美妆客服共情辅助系统"
requires-python = ">=3.12"
dependencies = [
    "openpyxl>=3.1",
    "python-dotenv>=1.0",
    "openai>=1.40",
    "pandas>=2.2",
    "pillow>=10.0",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

- [ ] **Step 2: 安装依赖**

Run: `uv pip install --python .venv/bin/python openpyxl python-dotenv openai pandas pillow pytest`
Expected: 安装成功，无报错

> 不用 `pip install -e .`：`core/`、`etl/`、`scripts/` 是多个平铺顶层包，setuptools 自动发现会失败；
> `[tool.pytest.ini_options] pythonpath = ["."]` 已保证 import 可用。

- [ ] **Step 3: 写失败的测试**

```python
# tests/test_config.py
from pathlib import Path
from core import config


def test_project_root_contains_pyproject():
    assert (config.PROJECT_ROOT / "pyproject.toml").is_file()


def test_source_xlsx_exists():
    assert config.SOURCE_XLSX.is_file(), f"官方数据源缺失: {config.SOURCE_XLSX}"


def test_db_path_under_data_dir():
    assert config.DB_PATH.parent == config.DATA_DIR
    assert config.DB_PATH.name == "app.db"


def test_load_env_sets_api_key(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    config.load_env()
    import os
    assert os.environ["DASHSCOPE_API_KEY"].startswith("sk-")
```

- [ ] **Step 4: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core'`

- [ ] **Step 5: 写实现**

```python
# core/__init__.py
```

```python
# core/config.py
"""路径常量与环境变量加载。"""
from pathlib import Path

from dotenv import load_dotenv as _load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "app.db"
MOCK_IMAGE_DIR = DATA_DIR / "mock_images"
ENV_PATH = PROJECT_ROOT / ".env"

SOURCE_XLSX = Path(
    "/Users/wenbiming/Documents/misc/AI-Assist/赛题 1：数据共情者-业务数据.xlsx"
)


def load_env() -> None:
    """加载 .env 到进程环境变量。显式传路径，避免 dotenv 在 stdin 场景下的栈探测失败。"""
    _load_dotenv(ENV_PATH)
```

```python
# tests/__init__.py
```

- [ ] **Step 6: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_config.py -v`
Expected: 4 passed

- [ ] **Step 7: 提交**

```bash
git add pyproject.toml core/ tests/
git commit -m "feat(core): 项目骨架与路径配置"
```

---

### Task 2: Excel 读取与列名映射

**Files:**
- Create: `etl/__init__.py`
- Create: `etl/schema.py`
- Create: `etl/reader.py`
- Test: `tests/test_reader.py`

**Interfaces:**
- Consumes: `core.config.SOURCE_XLSX`
- Produces:
  - `etl.schema.TABLES: dict[str, TableSpec]` — 键为英文表名
  - `etl.schema.TableSpec` — dataclass，字段 `sheet: str`、`columns: dict[str, str]`（中文列名 → 英文列名）、`pk: str | None`
  - `etl.reader.read_sheet(table: str) -> list[dict]` — 返回英文键的行字典列表

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_reader.py
from etl import reader, schema


def test_all_seven_tables_declared():
    assert set(schema.TABLES) == {
        "chat", "orders",
        "ticket_reissue", "ticket_payout", "ticket_logistics",
        "ticket_adverse", "ticket_return",
    }


def test_row_counts_match_baseline():
    expected = {
        "chat": 998, "orders": 113,
        "ticket_reissue": 24, "ticket_payout": 13, "ticket_logistics": 15,
        "ticket_adverse": 10, "ticket_return": 18,
    }
    for table, n in expected.items():
        assert len(reader.read_sheet(table)) == n, f"{table} 行数不符"


def test_chat_row_has_english_keys():
    row = reader.read_sheet("chat")[0]
    assert row["session_id"] == "S00001"
    assert row["role"] == "买家"
    assert row["buyer"] == "邓e**"
    assert row["scene_major"] == "补发换货"
    assert row["scene_minor"] == "破损换货"
    assert row["order_no"] == "6920185815517983396"


def test_ticket_status_columns_normalised_to_status():
    """补发换货用「工单状态」，不良反应用「任务状态」，统一映射为 status。"""
    assert reader.read_sheet("ticket_reissue")[0]["status"] in {"已完结", "进行中"}
    assert reader.read_sheet("ticket_adverse")[0]["status"] in {
        "已完结", "待处理", "处理中",
    }


def test_every_declared_column_exists_in_sheet():
    """列名映射写错会静默产生全 None 列，必须显式挡住。"""
    for table, spec in schema.TABLES.items():
        rows = reader.read_sheet(table)
        for eng in spec.columns.values():
            assert eng in rows[0], f"{table}.{eng} 缺失"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_reader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'etl'`

- [ ] **Step 3: 写 schema.py**

```python
# etl/__init__.py
```

```python
# etl/schema.py
"""7 张原表的中英列名映射。原表 1:1 映射 Excel，不做清洗。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class TableSpec:
    sheet: str                    # Excel sheet 名
    columns: dict[str, str]       # 中文列名 -> 英文列名
    pk: str | None                # 主键（英文列名）


TABLES: dict[str, TableSpec] = {
    "chat": TableSpec(
        sheet="聊天记录",
        columns={
            "会话ID": "session_id", "消息序号": "seq", "message_id": "message_id",
            "发送时间": "sent_at", "角色": "role", "买家昵称": "buyer",
            "发送方": "sender", "店铺": "shop",
            "scene_major": "scene_major", "scene_minor": "scene_minor",
            "is_target_buyer_message": "is_target_buyer_message",
            "message_text": "message_text", "内容类型": "content_type",
            "chat_content": "chat_content", "category": "category",
            "image_path": "image_path",
            "关联订单号": "order_no", "关联工单号": "ticket_no",
        },
        pk="message_id",
    ),
    "orders": TableSpec(
        sheet="订单",
        columns={
            "订单号": "order_no", "会话ID": "session_id", "买家昵称": "buyer",
            "店铺": "shop", "商品货号": "sku", "商品名称": "item_name",
            "数量": "qty", "单价(元)": "unit_price", "实付金额(元)": "paid_amount",
            "订单状态": "order_status", "下单时间": "created_at",
            "付款时间": "paid_at", "发货时间": "shipped_at",
            "快递公司": "carrier", "物流单号": "tracking_no",
            "收货省": "province", "收货市": "city",
            "赠品": "gift", "买家留言": "buyer_note",
        },
        pk="order_no",
    ),
    "ticket_reissue": TableSpec(
        sheet="补发换货工单",
        columns={
            "工单号": "ticket_no", "会话ID": "session_id",
            "关联订单号": "order_no", "买家昵称": "buyer", "店铺": "shop",
            "工单类型": "ticket_type", "售后原因": "reason",
            "发出商品货号": "sku", "发出商品名称": "item_name", "数量": "qty",
            "原订单物流单号": "orig_tracking_no", "补发物流单号": "reissue_tracking_no",
            "快递公司": "carrier", "发货仓库": "warehouse",
            "客诉加急": "urgent", "工单状态": "status", "处理人": "handler",
            "创建时间": "created_at", "完成时间": "finished_at",
        },
        pk="ticket_no",
    ),
    "ticket_payout": TableSpec(
        sheet="线下打款工单",
        columns={
            "工单号": "ticket_no", "会话ID": "session_id",
            "关联订单号": "order_no", "买家昵称": "buyer", "店铺": "shop",
            "打款类型": "payout_type", "退款问题类型": "reason",
            "退款金额(元)": "amount", "支付宝实名": "alipay_name",
            "支付宝账号": "alipay_account", "相关物流单号": "tracking_no",
            "转账状态": "transfer_status", "工单状态": "status",
            "处理人": "handler", "创建时间": "created_at", "完成时间": "finished_at",
        },
        pk="ticket_no",
    ),
    "ticket_logistics": TableSpec(
        sheet="物流工单",
        columns={
            "工单号": "ticket_no", "会话ID": "session_id",
            "关联订单号": "order_no", "买家昵称": "buyer", "店铺": "shop",
            "问题类型": "reason", "快递公司": "carrier",
            "问题包裹物流单号": "tracking_no", "发货仓": "warehouse",
            "订单实付(元)": "paid_amount", "处理方案": "solution",
            "收货省": "province", "收货市": "city",
            "工单状态": "status", "处理人": "handler",
            "创建时间": "created_at", "完成时间": "finished_at",
        },
        pk="ticket_no",
    ),
    "ticket_adverse": TableSpec(
        sheet="不良反应工单",
        columns={
            "工单号": "ticket_no", "会话ID": "session_id",
            "关联订单号": "order_no", "买家昵称": "buyer", "店铺": "shop",
            "类型": "channel", "年龄": "age", "肤质": "skin_type",
            "使用商品": "item_name", "产品批次号": "batch_no",
            "不适部位": "affected_area", "症状描述": "symptom",
            "用后多久出现": "onset", "是否停用": "stopped_use",
            "是否就医": "sought_care", "任务状态": "status",
            "处理人": "handler", "创建时间": "created_at", "完成时间": "finished_at",
        },
        pk="ticket_no",
    ),
    "ticket_return": TableSpec(
        sheet="售后退货工单",
        columns={
            "工单号": "ticket_no", "会话ID": "session_id",
            "关联订单号": "order_no", "买家昵称": "buyer", "店铺": "shop",
            "包裹类型": "parcel_type", "退货原因": "reason",
            "退货物流单号": "tracking_no", "快递公司": "carrier",
            "退款编号": "refund_no", "签收建议": "signoff_advice",
            "是否异常": "abnormal", "任务状态": "status",
            "处理人": "handler", "创建时间": "created_at", "完成时间": "finished_at",
        },
        pk="ticket_no",
    ),
}

TICKET_TABLES = [
    "ticket_reissue", "ticket_payout", "ticket_logistics",
    "ticket_adverse", "ticket_return",
]

CLOSED_STATUS = "已完结"
```

- [ ] **Step 4: 写 reader.py**

```python
# etl/reader.py
"""Excel → list[dict]（英文键）。"""
from functools import lru_cache

import openpyxl

from core.config import SOURCE_XLSX
from etl.schema import TABLES


@lru_cache(maxsize=1)
def _workbook():
    return openpyxl.load_workbook(SOURCE_XLSX, data_only=True, read_only=True)


def read_sheet(table: str) -> list[dict]:
    """读一张原表。缺失的声明列会抛错，避免静默产生全 None 列。"""
    spec = TABLES[table]
    ws = _workbook()[spec.sheet]
    rows = ws.iter_rows(values_only=True)
    header = list(next(rows))

    missing = set(spec.columns) - set(header)
    if missing:
        raise KeyError(f"{table}: Excel 中缺少声明的列 {sorted(missing)}")

    pos = {cn: header.index(cn) for cn in spec.columns}
    out = []
    for row in rows:
        if row[0] is None:          # 尾部空行
            continue
        out.append({eng: row[pos[cn]] for cn, eng in spec.columns.items()})
    return out
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_reader.py -v`
Expected: 5 passed

- [ ] **Step 6: 提交**

```bash
git add etl/ tests/test_reader.py
git commit -m "feat(etl): Excel 读取与 7 张表的中英列名映射"
```

---

### Task 3: SQLite 建表与幂等灌数

**Files:**
- Create: `etl/db.py`
- Create: `etl/loader.py`
- Test: `tests/test_loader.py`

**Interfaces:**
- Consumes: `etl.schema.TABLES`、`etl.reader.read_sheet`
- Produces:
  - `etl.db.connect(path: Path | None = None) -> sqlite3.Connection` — 行工厂为 `sqlite3.Row`，已开启外键
  - `etl.db.create_tables(conn) -> None` — 建 7 张原表 + `buyer_profile` + `scene_map` + `session_summary` + `risk_event` + `promise`
  - `etl.loader.load_raw_tables(conn) -> dict[str, int]` — 返回每张表灌入行数

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_loader.py
import pytest

from etl import db, loader

BASELINE = {
    "chat": 998, "orders": 113,
    "ticket_reissue": 24, "ticket_payout": 13, "ticket_logistics": 15,
    "ticket_adverse": 10, "ticket_return": 18,
}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.create_tables(c)
    yield c
    c.close()


def test_all_tables_created(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(BASELINE) <= names
    assert {"buyer_profile", "scene_map", "session_summary",
            "risk_event", "promise"} <= names


def test_load_row_counts(conn):
    counts = loader.load_raw_tables(conn)
    assert counts == BASELINE


def test_load_is_idempotent(conn):
    loader.load_raw_tables(conn)
    first = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
             for t in BASELINE}
    loader.load_raw_tables(conn)
    second = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in BASELINE}
    assert first == second == BASELINE


def test_chat_values_preserved_verbatim(conn):
    loader.load_raw_tables(conn)
    row = conn.execute(
        "SELECT * FROM chat WHERE message_id = '20974795108539.PNM'").fetchone()
    assert row["session_id"] == "S00001"
    assert row["sent_at"] == "2026-05-05 10:18:45"
    assert "泵头是坏的" in row["message_text"]


def test_open_tickets_total_is_28(conn):
    loader.load_raw_tables(conn)
    total = sum(
        conn.execute(
            f"SELECT COUNT(*) c FROM {t} WHERE status != '已完结'").fetchone()["c"]
        for t in ["ticket_reissue", "ticket_payout", "ticket_logistics",
                  "ticket_adverse", "ticket_return"]
    )
    assert total == 28
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_loader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'etl.db'`

- [ ] **Step 3: 写 db.py**

```python
# etl/db.py
"""SQLite 连接与建表。所有列一律 TEXT，保持与 Excel 原值一致（spec §3.2 不做清洗）。"""
import sqlite3
from pathlib import Path

from core.config import DB_PATH
from etl.schema import TABLES

# 派生表 DDL。session_summary / risk_event / promise 在 M1 只建表，由 M2 填充。
DERIVED_DDL = [
    """
    CREATE TABLE IF NOT EXISTS buyer_profile (
        buyer            TEXT PRIMARY KEY,
        session_count    INTEGER NOT NULL,
        order_count      INTEGER NOT NULL,
        total_paid       REAL    NOT NULL,
        ticket_count     INTEGER NOT NULL,
        open_ticket_count INTEGER NOT NULL,
        scene_dist       TEXT    NOT NULL,   -- JSON: {scene_major: 次数}
        first_contact_at TEXT,
        last_contact_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scene_map (
        scene_minor TEXT PRIMARY KEY,
        scene_major TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_summary (
        session_id       TEXT PRIMARY KEY,
        summary          TEXT,
        scene_major      TEXT,
        scene_minor      TEXT,
        intent_confidence REAL,
        emotion          INTEGER,
        emotion_trend    TEXT,
        risk_tags        TEXT,      -- JSON 数组
        suggested_actions TEXT,     -- JSON 数组
        model            TEXT,
        tokens_in        INTEGER,
        tokens_out       INTEGER,
        updated_at       TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS risk_event (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        risk_type   TEXT NOT NULL,
        level       TEXT NOT NULL,
        session_id  TEXT,
        buyer       TEXT,
        ticket_no   TEXT,
        detected_by TEXT NOT NULL,   -- L0 / L1 / L2
        status      TEXT NOT NULL,   -- 待处理 / 跟进中 / 已闭环
        handler     TEXT,
        detail      TEXT,
        created_at  TEXT,
        updated_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS promise (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id    TEXT NOT NULL,
        session_id    TEXT NOT NULL,
        buyer         TEXT NOT NULL,
        promise_text  TEXT NOT NULL,
        promise_type  TEXT,
        made_at       TEXT NOT NULL,
        deadline_at   TEXT,
        ticket_no     TEXT,
        closed        INTEGER NOT NULL DEFAULT 0,
        overdue       INTEGER NOT NULL DEFAULT 0
    )
    """,
]

INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_chat_session ON chat(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_chat_buyer ON chat(buyer)",
    "CREATE INDEX IF NOT EXISTS idx_orders_buyer ON orders(buyer)",
    "CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(session_id)",
]


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path is not None else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables(conn: sqlite3.Connection) -> None:
    for table, spec in TABLES.items():
        cols = []
        for eng in spec.columns.values():
            cols.append(f"{eng} TEXT PRIMARY KEY" if eng == spec.pk else f"{eng} TEXT")
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(cols)})")
    for ddl in DERIVED_DDL:
        conn.execute(ddl)
    for ddl in INDEX_DDL:
        conn.execute(ddl)
    conn.commit()
```

- [ ] **Step 4: 写 loader.py**

```python
# etl/loader.py
"""幂等灌数：每次 DELETE 后全量重灌，保证重复执行结果一致（spec §3.5）。"""
import sqlite3

from etl.reader import read_sheet
from etl.schema import TABLES


def load_raw_tables(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table, spec in TABLES.items():
        rows = read_sheet(table)
        cols = list(spec.columns.values())
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(f"DELETE FROM {table}")
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
            [tuple(None if r[c] is None else str(r[c]) for c in cols) for r in rows],
        )
        counts[table] = len(rows)
    conn.commit()
    return counts
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_loader.py -v`
Expected: 5 passed

- [ ] **Step 6: 提交**

```bash
git add etl/db.py etl/loader.py tests/test_loader.py
git commit -m "feat(etl): SQLite 建表与幂等灌数"
```

---

### Task 4: 派生表 buyer_profile 与 scene_map

**Files:**
- Create: `etl/derive.py`
- Test: `tests/test_derive.py`

**Interfaces:**
- Consumes: `etl.db.connect`、`etl.loader.load_raw_tables`
- Produces:
  - `etl.derive.build_scene_map(conn) -> int` — 写入 `scene_map`，返回行数
  - `etl.derive.build_buyer_profile(conn) -> int` — 写入 `buyer_profile`，返回行数
  - `etl.derive.build_all(conn) -> dict[str, int]`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_derive.py
import json

import pytest

from etl import db, derive, loader


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.create_tables(c)
    loader.load_raw_tables(c)
    yield c
    c.close()


def test_scene_map_is_41_rows(conn):
    assert derive.build_scene_map(conn) == 41


def test_scene_map_is_one_to_one(conn):
    """41 个 scene_minor 严格映射唯一 scene_major（spec §2.6 的核心发现）。"""
    derive.build_scene_map(conn)
    rows = conn.execute("SELECT scene_minor, scene_major FROM scene_map").fetchall()
    assert len(rows) == 41
    assert len({r["scene_major"] for r in rows}) == 10
    assert dict(conn.execute(
        "SELECT scene_minor, scene_major FROM scene_map"
    ).fetchall())["退款迟迟不到账"] == "订单服务"


def test_buyer_profile_is_112_rows(conn):
    assert derive.build_buyer_profile(conn) == 112


def test_buyer_profile_of_demo_sample(conn):
    """演示金样本 魏h**：3 会话 / 3 订单 / 实付 556 元 / 1 张未完结工单。"""
    derive.build_buyer_profile(conn)
    r = conn.execute(
        "SELECT * FROM buyer_profile WHERE buyer = ?", ("魏h**",)).fetchone()
    assert r["session_count"] == 3
    assert r["order_count"] == 3
    assert r["total_paid"] == pytest.approx(556.0)
    assert r["ticket_count"] == 1
    assert r["open_ticket_count"] == 1
    assert r["first_contact_at"] == "2026-05-05 12:05:11"
    assert r["last_contact_at"] == "2026-05-09 11:34:05"
    assert set(json.loads(r["scene_dist"])) == {"订单服务", "售后退货", "物流服务"}


def test_repeat_contact_buyers_count(conn):
    """21% 重复进线率：22 个买家 2 次 + 2 个买家 3 次 = 24 个。"""
    derive.build_buyer_profile(conn)
    n = conn.execute(
        "SELECT COUNT(*) c FROM buyer_profile WHERE session_count > 1").fetchone()["c"]
    assert n == 24


def test_build_all_is_idempotent(conn):
    first = derive.build_all(conn)
    second = derive.build_all(conn)
    assert first == second == {"scene_map": 41, "buyer_profile": 112}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_derive.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'etl.derive'`

- [ ] **Step 3: 写 derive.py**

```python
# etl/derive.py
"""派生表构建。全部幂等：先 DELETE 再全量重建。"""
import json
import sqlite3
from collections import Counter, defaultdict

from etl.schema import TICKET_TABLES


def build_scene_map(conn: sqlite3.Connection) -> int:
    """scene_minor -> scene_major 的确定性映射表（spec §2.6 / §4.3）。"""
    pairs = conn.execute(
        "SELECT DISTINCT scene_minor, scene_major FROM chat "
        "WHERE scene_minor IS NOT NULL"
    ).fetchall()

    seen: dict[str, str] = {}
    for r in pairs:
        minor, major = r["scene_minor"], r["scene_major"]
        if minor in seen and seen[minor] != major:
            raise ValueError(
                f"scene_minor「{minor}」映射到多个 major: {seen[minor]} / {major}；"
                "spec §4.3 的 minor-only 策略前提被破坏"
            )
        seen[minor] = major

    conn.execute("DELETE FROM scene_map")
    conn.executemany(
        "INSERT INTO scene_map (scene_minor, scene_major) VALUES (?, ?)",
        sorted(seen.items()),
    )
    conn.commit()
    return len(seen)


def _ticket_stats(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """买家 -> (工单总数, 未完结工单数)。跨 5 张工单表聚合。"""
    total: Counter = Counter()
    open_: Counter = Counter()
    for table in TICKET_TABLES:
        for r in conn.execute(f"SELECT buyer, status FROM {table}"):
            total[r["buyer"]] += 1
            if r["status"] != "已完结":
                open_[r["buyer"]] += 1
    return {b: (total[b], open_[b]) for b in total}


def build_buyer_profile(conn: sqlite3.Connection) -> int:
    sessions: dict[str, set] = defaultdict(set)
    scenes: dict[str, Counter] = defaultdict(Counter)
    first: dict[str, str] = {}
    last: dict[str, str] = {}

    for r in conn.execute(
        "SELECT buyer, session_id, scene_major, sent_at FROM chat ORDER BY sent_at"
    ):
        b = r["buyer"]
        sessions[b].add(r["session_id"])
        if r["scene_major"]:
            scenes[b][r["scene_major"]] += 1
        first.setdefault(b, r["sent_at"])
        last[b] = r["sent_at"]

    orders: dict[str, list[float]] = defaultdict(list)
    for r in conn.execute("SELECT buyer, paid_amount FROM orders"):
        orders[r["buyer"]].append(float(r["paid_amount"] or 0))

    tickets = _ticket_stats(conn)

    rows = []
    for b in sessions:
        t_total, t_open = tickets.get(b, (0, 0))
        rows.append((
            b, len(sessions[b]), len(orders.get(b, [])), sum(orders.get(b, [])),
            t_total, t_open,
            json.dumps(dict(scenes[b]), ensure_ascii=False),
            first.get(b), last.get(b),
        ))

    conn.execute("DELETE FROM buyer_profile")
    conn.executemany(
        "INSERT INTO buyer_profile (buyer, session_count, order_count, total_paid,"
        " ticket_count, open_ticket_count, scene_dist, first_contact_at,"
        " last_contact_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def build_all(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "scene_map": build_scene_map(conn),
        "buyer_profile": build_buyer_profile(conn),
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_derive.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add etl/derive.py tests/test_derive.py
git commit -m "feat(etl): buyer_profile 与 scene_map 派生表"
```

---

### Task 5: 数据质量报告与昵称碰撞检查

**Files:**
- Create: `etl/quality.py`
- Test: `tests/test_quality.py`

**Interfaces:**
- Consumes: `etl.db.connect`、`etl.loader.load_raw_tables`、`etl.derive.build_all`
- Produces:
  - `etl.quality.QualityReport` — dataclass，字段见下
  - `etl.quality.check(conn) -> QualityReport`
  - `etl.quality.format_report(report: QualityReport) -> str`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_quality.py
import pytest

from etl import db, derive, loader, quality


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.create_tables(c)
    loader.load_raw_tables(c)
    derive.build_all(c)
    yield c
    c.close()


def test_report_headline_numbers(conn):
    r = quality.check(conn)
    assert r.session_count == 138
    assert r.buyer_count == 112
    assert r.message_count == 998
    assert r.order_count == 113
    assert r.open_ticket_count == 28


def test_orphan_links_are_zero(conn):
    """spec §2.1：工单/订单的会话ID 100% 可回连聊天。"""
    r = quality.check(conn)
    assert r.orphan_order_sessions == []
    assert r.orphan_ticket_sessions == []


def test_sessions_without_order_or_ticket(conn):
    """spec §2.1：25 个纯售前咨询会话，插件需走降级模式。"""
    r = quality.check(conn)
    assert len(r.consult_only_sessions) == 25


def test_nickname_collision_suspects_listed(conn):
    """spec §2.5.3：昵称是唯一身份键，需列出可疑碰撞供人工核对。"""
    r = quality.check(conn)
    assert isinstance(r.collision_suspects, list)
    for s in r.collision_suspects:
        assert set(s) >= {"buyer", "provinces", "session_count"}


def test_format_report_mentions_mock_disclaimer(conn):
    """spec §2.5.4：MOCK 声明必须保留在所有产出物中。"""
    text = quality.format_report(quality.check(conn))
    assert "MOCK" in text
    assert "138" in text and "112" in text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_quality.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'etl.quality'`

- [ ] **Step 3: 写 quality.py**

```python
# etl/quality.py
"""数据质量报告。ETL 每次运行都产出，用于及早发现数据假设被打破。"""
import sqlite3
from dataclasses import dataclass, field

from etl.schema import TICKET_TABLES

MOCK_DISCLAIMER = (
    "⚠ 本数据集全部内容为官方提供的 AI 生成虚构 MOCK 数据，"
    "与任何真实企业、品牌、个人或交易无关，不得用于生产用途。"
)


@dataclass
class QualityReport:
    session_count: int
    buyer_count: int
    message_count: int
    order_count: int
    ticket_count: int
    open_ticket_count: int
    orphan_order_sessions: list[str]
    orphan_ticket_sessions: list[str]
    consult_only_sessions: list[str]
    collision_suspects: list[dict] = field(default_factory=list)


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    return conn.execute(sql).fetchone()[0]


def check(conn: sqlite3.Connection) -> QualityReport:
    chat_sessions = {
        r["session_id"] for r in conn.execute("SELECT DISTINCT session_id FROM chat")
    }
    order_sessions = {
        r["session_id"] for r in conn.execute("SELECT DISTINCT session_id FROM orders")
    }
    ticket_sessions: set[str] = set()
    ticket_total = 0
    open_total = 0
    for t in TICKET_TABLES:
        for r in conn.execute(f"SELECT session_id, status FROM {t}"):
            ticket_sessions.add(r["session_id"])
            ticket_total += 1
            if r["status"] != "已完结":
                open_total += 1

    # 昵称碰撞嫌疑：同一昵称的订单收货省份多于 1 个
    suspects = []
    rows = conn.execute(
        "SELECT buyer, GROUP_CONCAT(DISTINCT province) provs FROM orders"
        " WHERE province IS NOT NULL GROUP BY buyer"
    ).fetchall()
    profile = dict(conn.execute(
        "SELECT buyer, session_count FROM buyer_profile").fetchall())
    for r in rows:
        provs = sorted(set((r["provs"] or "").split(",")))
        if len(provs) > 1:
            suspects.append({
                "buyer": r["buyer"],
                "provinces": provs,
                "session_count": profile.get(r["buyer"], 0),
            })

    return QualityReport(
        session_count=len(chat_sessions),
        buyer_count=_scalar(conn, "SELECT COUNT(DISTINCT buyer) FROM chat"),
        message_count=_scalar(conn, "SELECT COUNT(*) FROM chat"),
        order_count=_scalar(conn, "SELECT COUNT(*) FROM orders"),
        ticket_count=ticket_total,
        open_ticket_count=open_total,
        orphan_order_sessions=sorted(order_sessions - chat_sessions),
        orphan_ticket_sessions=sorted(ticket_sessions - chat_sessions),
        consult_only_sessions=sorted(
            chat_sessions - order_sessions - ticket_sessions),
    )


def format_report(report: QualityReport) -> str:
    lines = [
        MOCK_DISCLAIMER,
        "",
        "=== 数据质量报告 ===",
        f"会话 {report.session_count} | 买家 {report.buyer_count} "
        f"| 消息 {report.message_count} | 订单 {report.order_count}",
        f"工单 {report.ticket_count}（未完结 {report.open_ticket_count}）",
        f"孤儿订单会话: {len(report.orphan_order_sessions)}",
        f"孤儿工单会话: {len(report.orphan_ticket_sessions)}",
        f"纯咨询会话（无单无工单，走插件降级模式）: "
        f"{len(report.consult_only_sessions)}",
        f"昵称碰撞嫌疑（同昵称跨省收货，需人工核对）: "
        f"{len(report.collision_suspects)}",
    ]
    for s in report.collision_suspects:
        lines.append(
            f"   - {s['buyer']}: {'/'.join(s['provinces'])} "
            f"（{s['session_count']} 个会话）")
    return "\n".join(lines)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_quality.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add etl/quality.py tests/test_quality.py
git commit -m "feat(etl): 数据质量报告与昵称碰撞检查"
```

---

### Task 6: timeline 统一事件流

**Files:**
- Create: `core/timeline.py`
- Test: `tests/test_timeline.py`

**Interfaces:**
- Consumes: `etl.db.connect`（只读使用）
- Produces:
  - `core.timeline.TimelineEvent` — dataclass：`ts: str`、`kind: str`（`"chat" | "order" | "ticket" | "promise"`）、`session_id: str | None`、`buyer: str`、`title: str`、`detail: str`、`ref_id: str | None`、`is_open: bool`
  - `core.timeline.buyer_timeline(conn, buyer: str) -> list[TimelineEvent]` — 跨会话，按 `ts` 升序
  - `core.timeline.session_timeline(conn, session_id: str) -> list[TimelineEvent]` — 单会话，按 `ts` 升序

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_timeline.py
import pytest

from core import timeline
from etl import db, derive, loader


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.create_tables(c)
    loader.load_raw_tables(c)
    derive.build_all(c)
    yield c
    c.close()


def test_buyer_timeline_is_chronological(conn):
    evs = timeline.buyer_timeline(conn, "魏h**")
    assert evs == sorted(evs, key=lambda e: e.ts)


def test_buyer_timeline_spans_three_sessions(conn):
    """跨会话全轨迹还原是「信息孤岛」的解药（spec §3.4）。"""
    evs = timeline.buyer_timeline(conn, "魏h**")
    chat = [e for e in evs if e.kind == "chat"]
    assert len(chat) == 21
    assert {e.session_id for e in chat} == {"S00005", "S00059", "S00099"}


def test_buyer_timeline_includes_all_three_kinds(conn):
    evs = timeline.buyer_timeline(conn, "魏h**")
    assert {e.kind for e in evs} >= {"chat", "order", "ticket"}


def test_promise_events_absent_until_m2(conn):
    """promise 表在 M1 为空，timeline 必须能安全处理空表（M2 填充后自动出现）。"""
    evs = timeline.buyer_timeline(conn, "魏h**")
    assert [e for e in evs if e.kind == "promise"] == []


def test_open_ticket_flagged(conn):
    """未完结的风控工单必须被标出，这是插件头卡的风险徽章来源。"""
    evs = timeline.buyer_timeline(conn, "魏h**")
    t = [e for e in evs if e.kind == "ticket" and e.ref_id == "KOC7263722"]
    assert t, "未找到风控工单 KOC7263722"
    assert any(e.is_open for e in t)


def test_session_timeline_only_that_session(conn):
    evs = timeline.session_timeline(conn, "S00005")
    assert evs
    assert all(e.session_id == "S00005" for e in evs)


def test_consult_only_session_has_chat_events_only(conn):
    """25 个纯咨询会话无订单无工单，timeline 不应报错，只返回聊天事件。"""
    evs = timeline.session_timeline(conn, "S00002")
    assert evs
    assert {e.kind for e in evs} == {"chat"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_timeline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.timeline'`

- [ ] **Step 3: 写 timeline.py**

```python
# core/timeline.py
"""统一事件流：把聊天 / 订单 / 工单按时间归并成一条跨会话服务轨迹。

这是「多源跨维度融合」的技术落点，也是插件第一屏与看板下钻的共同数据源
（spec §3.4）。只读，不写库。
"""
import sqlite3
from dataclasses import dataclass

from etl.schema import TICKET_TABLES

CLOSED = "已完结"

# 工单表 -> 展示用中文名
TICKET_LABEL = {
    "ticket_reissue": "补发换货工单",
    "ticket_payout": "线下打款工单",
    "ticket_logistics": "物流工单",
    "ticket_adverse": "不良反应工单",
    "ticket_return": "售后退货工单",
}


@dataclass(frozen=True)
class TimelineEvent:
    ts: str
    kind: str                 # "chat" | "order" | "ticket" | "promise"
    session_id: str | None
    buyer: str
    title: str
    detail: str
    ref_id: str | None
    is_open: bool = False


def _chat_events(rows) -> list[TimelineEvent]:
    return [
        TimelineEvent(
            ts=r["sent_at"], kind="chat", session_id=r["session_id"],
            buyer=r["buyer"], title=r["role"],
            detail=r["message_text"] or "", ref_id=r["message_id"],
        )
        for r in rows
    ]


def _order_events(rows) -> list[TimelineEvent]:
    out = []
    for r in rows:
        for col, label in (("created_at", "下单"), ("paid_at", "付款"),
                           ("shipped_at", "发货")):
            if not r[col]:
                continue
            out.append(TimelineEvent(
                ts=r[col], kind="order", session_id=r["session_id"],
                buyer=r["buyer"], title=f"订单{label}",
                detail=f"{r['item_name']} ×{r['qty']} 实付{r['paid_amount']}元"
                       f"（{r['order_status']}）",
                ref_id=r["order_no"],
            ))
    return out


def _ticket_events(conn: sqlite3.Connection, where: str, param: str
                   ) -> list[TimelineEvent]:
    out = []
    for table in TICKET_TABLES:
        label = TICKET_LABEL[table]
        for r in conn.execute(
            f"SELECT * FROM {table} WHERE {where} = ?", (param,)
        ):
            is_open = r["status"] != CLOSED
            keys = r.keys()
            reason = (r["reason"] if "reason" in keys
                      else r["symptom"] if "symptom" in keys else "")
            out.append(TimelineEvent(
                ts=r["created_at"], kind="ticket", session_id=r["session_id"],
                buyer=r["buyer"], title=f"创建{label}",
                detail=f"{reason or ''}（{r['status']}）".strip("（）"),
                ref_id=r["ticket_no"], is_open=is_open,
            ))
            if r["finished_at"]:
                out.append(TimelineEvent(
                    ts=r["finished_at"], kind="ticket",
                    session_id=r["session_id"], buyer=r["buyer"],
                    title=f"{label}完结", detail=reason or "",
                    ref_id=r["ticket_no"], is_open=False,
                ))
    return out


def _promise_events(conn: sqlite3.Connection, where: str, param: str
                    ) -> list[TimelineEvent]:
    """承诺事件（spec §3.4）。M1 中 promise 表为空，M2 填充后自动生效。"""
    return [
        TimelineEvent(
            ts=r["made_at"], kind="promise", session_id=r["session_id"],
            buyer=r["buyer"],
            title="客服承诺" + ("（已逾期）" if r["overdue"] else ""),
            detail=f"{r['promise_text']}"
                   + (f" · 期限 {r['deadline_at']}" if r["deadline_at"] else ""),
            ref_id=r["message_id"], is_open=not r["closed"],
        )
        for r in conn.execute(f"SELECT * FROM promise WHERE {where} = ?", (param,))
    ]


def buyer_timeline(conn: sqlite3.Connection, buyer: str) -> list[TimelineEvent]:
    """该买家的跨会话全轨迹，按时间升序。"""
    evs = _chat_events(
        conn.execute("SELECT * FROM chat WHERE buyer = ?", (buyer,)))
    evs += _order_events(
        conn.execute("SELECT * FROM orders WHERE buyer = ?", (buyer,)))
    evs += _ticket_events(conn, "buyer", buyer)
    evs += _promise_events(conn, "buyer", buyer)
    return sorted(evs, key=lambda e: e.ts)


def session_timeline(conn: sqlite3.Connection, session_id: str
                     ) -> list[TimelineEvent]:
    """单个会话的事件流，按时间升序。"""
    evs = _chat_events(
        conn.execute("SELECT * FROM chat WHERE session_id = ?", (session_id,)))
    evs += _order_events(
        conn.execute("SELECT * FROM orders WHERE session_id = ?", (session_id,)))
    evs += _ticket_events(conn, "session_id", session_id)
    evs += _promise_events(conn, "session_id", session_id)
    return sorted(evs, key=lambda e: e.ts)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_timeline.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add core/timeline.py tests/test_timeline.py
git commit -m "feat(core): 跨会话统一事件流 timeline"
```

---

### Task 7: 生成 mock 图片

**Files:**
- Create: `scripts/gen_mock_images.py`
- Test: `tests/test_mock_images.py`

**Interfaces:**
- Consumes: `core.config.MOCK_IMAGE_DIR`、`etl.reader.read_sheet`
- Produces: `scripts.gen_mock_images.generate(out_root: Path) -> list[Path]` — 生成全部图片，返回路径列表

**背景：** 官方给了 29 条图片消息的 `image_path`（10 个语义目录：`refund_screenshot` 6 / `wrong_shade` 4 / `broken_parcel` 3 / `broken_pump` 3 / `live_promise` 3 / `missing_item` 3 / `short_item` 3 / `swatch` 2 / `consult_card` 1 / `logistics_screenshot` 1），但**未提供图片文件**。生成占位图使 M2 的 `qwen3-vl-flash` 链路可跑通（spec §2.5.1）。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_mock_images.py
from PIL import Image

from etl.reader import read_sheet
from scripts import gen_mock_images


def test_generates_one_file_per_image_path(tmp_path):
    paths = gen_mock_images.generate(tmp_path)
    declared = {r["image_path"] for r in read_sheet("chat") if r["image_path"]}
    assert len(declared) == 29
    assert len(paths) == 29


def test_paths_align_with_declared_image_path(tmp_path):
    """路径必须与 image_path 逐字对齐，否则 M2 的 VL 链路取不到图。"""
    gen_mock_images.generate(tmp_path)
    declared = {r["image_path"] for r in read_sheet("chat") if r["image_path"]}
    for rel in declared:
        assert (tmp_path / rel).is_file(), f"缺图: {rel}"


def test_generated_files_are_valid_images(tmp_path):
    paths = gen_mock_images.generate(tmp_path)
    for p in paths[:5]:
        with Image.open(p) as im:
            im.verify()


def test_ten_semantic_categories_present(tmp_path):
    gen_mock_images.generate(tmp_path)
    dirs = {d.name for d in (tmp_path / "mock_images").iterdir() if d.is_dir()}
    assert dirs == {
        "refund_screenshot", "wrong_shade", "broken_parcel", "broken_pump",
        "live_promise", "missing_item", "short_item", "swatch",
        "consult_card", "logistics_screenshot",
    }


def test_generate_is_idempotent(tmp_path):
    first = gen_mock_images.generate(tmp_path)
    second = gen_mock_images.generate(tmp_path)
    assert sorted(first) == sorted(second)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_mock_images.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts'`

- [ ] **Step 3: 写实现**

```python
# scripts/__init__.py
```

```python
# scripts/gen_mock_images.py
"""生成 mock 图片占位图，路径严格对齐聊天记录的 image_path（spec §2.5.1）。

官方未提供图片文件。这些图仅用于打通 qwen3-vl 链路，
内容为语义标签的文字卡片，不伪装成真实照片。
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from core.config import MOCK_IMAGE_DIR
from etl.reader import read_sheet

SIZE = (640, 480)

# 语义目录 -> (中文说明, 背景色)
CATEGORY = {
    "broken_pump": ("粉底液泵头破损", (214, 92, 92)),
    "broken_parcel": ("包裹外箱破损", (201, 106, 74)),
    "wrong_shade": ("色号发错", (176, 120, 190)),
    "missing_item": ("漏发赠品", (222, 158, 74)),
    "short_item": ("少发正装", (203, 145, 66)),
    "refund_screenshot": ("退款记录截图", (86, 132, 196)),
    "logistics_screenshot": ("物流轨迹截图", (74, 148, 158)),
    "live_promise": ("直播承诺截图", (196, 92, 148)),
    "swatch": ("试色对比图", (150, 122, 182)),
    "consult_card": ("咨询商品卡片", (104, 142, 110)),
}

DISCLAIMER = "MOCK — 虚构占位图，非真实照片"


def _draw(path: Path, category: str, stem: str) -> None:
    label, colour = CATEGORY[category]
    img = Image.new("RGB", SIZE, colour)
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, SIZE[0] - 20, SIZE[1] - 20], outline=(255, 255, 255), width=3)
    d.text((48, 180), label, fill=(255, 255, 255))
    d.text((48, 210), category, fill=(255, 255, 255))
    d.text((48, 240), stem, fill=(255, 255, 255))
    d.text((48, 420), DISCLAIMER, fill=(255, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=85)


def generate(out_root: Path | None = None) -> list[Path]:
    """为每个声明的 image_path 生成一张占位图。幂等：重复运行覆盖同名文件。"""
    root = Path(out_root) if out_root is not None else MOCK_IMAGE_DIR.parent

    declared = sorted({r["image_path"] for r in read_sheet("chat") if r["image_path"]})
    written = []
    for rel in declared:
        parts = Path(rel).parts          # ("mock_images", "<category>", "<file>.jpg")
        category = parts[1]
        if category not in CATEGORY:
            raise KeyError(f"未知语义目录 {category}（来自 {rel}），请补进 CATEGORY")
        target = root / rel
        _draw(target, category, Path(rel).stem)
        written.append(target)
    return written


if __name__ == "__main__":
    paths = generate()
    print(f"已生成 {len(paths)} 张 mock 图片 → {paths[0].parent.parent}")
    sys.exit(0)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_mock_images.py -v`
Expected: 5 passed

- [ ] **Step 5: 实际生成一份到 data/**

Run: `.venv/bin/python -m scripts.gen_mock_images`
Expected: `已生成 29 张 mock 图片 → .../data/mock_images`

- [ ] **Step 6: 提交**

```bash
git add scripts/ tests/test_mock_images.py
git commit -m "feat(scripts): 生成 29 张 mock 占位图，路径对齐 image_path"
```

---

### Task 8: ETL CLI 入口与端到端验证

**Files:**
- Create: `etl/build.py`
- Create: `README.md`
- Test: `tests/test_build.py`

**Interfaces:**
- Consumes: `etl.db`、`etl.loader`、`etl.derive`、`etl.quality`
- Produces: `etl.build.build(db_path: Path | None = None) -> quality.QualityReport`；命令行 `python -m etl.build`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_build.py
from core import timeline
from etl import build, db


def test_build_end_to_end(tmp_path):
    """一条命令从 Excel 到可查询的库。"""
    p = tmp_path / "app.db"
    report = build.build(p)
    assert report.session_count == 138
    assert report.buyer_count == 112
    assert report.open_ticket_count == 28
    assert p.is_file()


def test_built_db_supports_timeline_query(tmp_path):
    p = tmp_path / "app.db"
    build.build(p)
    conn = db.connect(p)
    try:
        evs = timeline.buyer_timeline(conn, "魏h**")
        assert len([e for e in evs if e.kind == "chat"]) == 21
        assert any(e.is_open for e in evs if e.kind == "ticket")
    finally:
        conn.close()


def test_build_is_idempotent(tmp_path):
    p = tmp_path / "app.db"
    first = build.build(p)
    second = build.build(p)
    assert first == second
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_build.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'etl.build'`

- [ ] **Step 3: 写 build.py**

```python
# etl/build.py
"""ETL 入口：Excel → SQLite → 派生表 → 质量报告。幂等。"""
import sys
from pathlib import Path

from etl import db, derive, loader, quality


def build(db_path: Path | None = None) -> quality.QualityReport:
    conn = db.connect(db_path)
    try:
        db.create_tables(conn)
        loader.load_raw_tables(conn)
        derive.build_all(conn)
        return quality.check(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    report = build()
    print(quality.format_report(report))
    sys.exit(0)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_build.py -v`
Expected: 3 passed

- [ ] **Step 5: 跑真实 ETL**

Run: `.venv/bin/python -m etl.build`
Expected: 打印 MOCK 声明 + 138/112/998/113 等数字 + 未完结 28 + 纯咨询 25

- [ ] **Step 6: 写 README.md**

```markdown
# 心迹 EmpathyTrace

欧莱雅集团第二届美妆科技黑客松 · 赛题一「数据共情者 — 消费者的AI管家」

> ⚠ 本项目使用的业务数据全部为官方提供的 AI 生成虚构 MOCK 数据，
> 与任何真实企业、品牌、个人或交易无关，不得用于生产用途。

## 快速开始

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python openpyxl python-dotenv openai pandas pillow pytest

# 生成 mock 图片
.venv/bin/python -m scripts.gen_mock_images

# 构建数据层
.venv/bin/python -m etl.build

# 跑测试
.venv/bin/python -m pytest -v
```

需要 `.env`（不入库）：

```
DASHSCOPE_API_KEY=sk-...
DASHSCOPE_BASE_URL=https://<workspace>.eu-central-1.maas.aliyuncs.com/compatible-mode/v1
```

## 文档

- 设计文档：`docs/superpowers/specs/2026-09-02-beauty-techathon-empathy-agent-design.md`
- M1 实现计划：`docs/superpowers/plans/2026-09-02-m1-data-layer.md`
```

- [ ] **Step 7: 跑全量测试**

Run: `.venv/bin/python -m pytest -v`
Expected: 全部通过（40 项）

- [ ] **Step 8: 提交**

```bash
git add etl/build.py README.md tests/test_build.py
git commit -m "feat(etl): ETL CLI 入口与端到端验证"
```

---

## M1 完成标准

- [ ] `.venv/bin/python -m etl.build` 一条命令跑通，输出质量报告
- [ ] `data/app.db` 生成，含 7 张原表 + 5 张派生表
- [ ] `data/mock_images/` 下 29 张图，路径与 `image_path` 逐字对齐
- [ ] `core.timeline.buyer_timeline(conn, "魏h**")` 返回跨 3 个会话的完整轨迹，未完结工单被标记
- [ ] `.venv/bin/python -m pytest` 全绿
- [ ] 质量报告中孤儿会话为 0、纯咨询会话为 25、未完结工单为 28

## 交给 M2 的接口

| 接口 | 用途 |
|---|---|
| `etl.db.connect()` | 拿连接 |
| `core.timeline.session_timeline(conn, sid)` | L1 的 prompt 输入 |
| `core.timeline.buyer_timeline(conn, buyer)` | L2 的风险归因输入 |
| `buyer_profile` 表 | L0 规则层的重复进线/未完结工单信号 |
| `scene_map` 表 | minor → major 反查（spec §4.3） |
| `session_summary` / `risk_event` / `promise` 空表 | L1/L2 的写入目标 |
