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


# --- 白名单按买家收窄（裁决 R5）------------------------------------------

def test_own_buyer_identifier_passes(conn):
    """本买家名下的真实订单号（S00099 买家 魏h**）放行。"""
    r = compliance.check_reply(
        conn, "S00099", "专业", "您的订单6920294005666782721已经安排发出了。")
    assert r.issues == [] and r.blocked is False


def test_other_buyers_real_identifier_is_foreign_and_blocked(conn):
    """真实存在但属于另一个买家的单号——比编造更危险。

    编造的号在快递官网查无此单；这个号查得出，查出的是别人的包裹，同时把另一
    买家的物流信息泄露给了本会话买家。全库并集白名单会放行它（实测 blocked=False）。
    暴露路径真实存在：retrieval.search_similar_cases 把其它会话的客服原话逐字
    塞进 L2 的 few-shot，而库内有 5 条客服消息带着真实运单号。
    """
    owner = conn.execute(
        "SELECT session_id FROM orders WHERE order_no='6920012167512343101'"
    ).fetchone()
    assert owner["session_id"] == "S00156", "语料变了，请换一个跨会话订单号"

    r = compliance.check_reply(
        conn, "S00099", "专业", "您的订单6920012167512343101已发出。")
    f = [i for i in r.issues if i.kind == compliance.KIND_FOREIGN_ID]
    assert len(f) == 1
    assert f[0].severity == compliance.SEV_BLOCK
    assert f[0].excerpt == "6920012167512343101"
    assert "不属于本会话买家" in f[0].detail
    assert r.blocked is True
    assert [i for i in r.issues if i.kind == compliance.KIND_FABRICATED_ID] == []


def test_buyer_identifiers_is_a_strict_subset(conn):
    mine = compliance.buyer_identifiers(conn, "S00099")
    assert mine, "本买家应当有标识符"
    assert mine < compliance.known_identifiers(conn), "买家白名单必须严格窄于全库"


# --- 大小写 / 分隔符 / 全角归一化（Important #4、Minor #10）-----------------

@pytest.mark.parametrize("text, excerpt", [
    ("顺丰单号 sf1234567890 请查收", "sf1234567890"),
    ("工单 koc9999999 已建", "koc9999999"),
    ("单号 SF-1234567890 已发", "SF-1234567890"),
    ("单号 SF 1234567890 已发", "SF 1234567890"),
    ("订单 1234-5678-9012 已发", "1234-5678-9012"),
    ("订单１２３４５６７８９０１２３４５６７８９已发", "１２３４５６７８９０１２３４５６７８９"),
])
def test_case_and_separator_variants_are_still_blocked(conn, text, excerpt):
    """漏报是本作品最贵的一种错：小写前缀、连字符、空格、全角数字都不能静默放过。"""
    r = compliance.check_reply(conn, "S00099", "专业", text)
    ids = [i for i in r.issues if i.kind == compliance.KIND_FABRICATED_ID]
    assert len(ids) == 1, f"漏报：{text}"
    assert ids[0].excerpt == excerpt
    assert text[ids[0].start:ids[0].end] == excerpt
    assert r.blocked is True


def test_real_identifier_in_lowercase_is_not_a_false_block(conn):
    """⚠️ 白名单必须一起归一化，否则真实单号写成小写会被误判成编造并阻断。"""
    r = compliance.check_reply(
        conn, "S00099", "专业", "您的退货工单 koc7263722 我这边正在盯办。")
    assert r.blocked is False, "白名单没做同样归一化——真单号被当成编造了"
    assert r.issues == []


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


def test_xiaojiejie_is_not_flagged(conn):
    """语料真实一例：「催财务小姐姐立刻操作退款」——第三方同事的口语称呼。

    与「仓库主管」是同一类误报：不是对买家的性别称谓，不该报警。
    """
    r = compliance.check_reply(
        conn, "S00099", "安抚", "我现在就去催财务小姐姐立刻给您操作退款。")
    assert [i for i in r.issues if i.kind == compliance.KIND_HONORIFIC] == []


def test_third_party_title_is_not_flagged(conn):
    """客服说「我联系了仓库主管」是合理信息，不是自称身份，不该报警。"""
    r = compliance.check_reply(
        conn, "S00099", "专业", "我已联系仓库主管为您单独锁定库存，并上报给仓储经理加急。")
    assert [i for i in r.issues if i.kind == compliance.KIND_HONORIFIC] == []


def test_new_promise_is_info_and_extracted(conn):
    r = compliance.check_reply(
        conn, "S00099", "专业", "您的订单我已加急标记，48小时内一定发出。")
    p = [i for i in r.issues if i.kind == compliance.KIND_NEW_PROMISE]
    assert len(p) == 1
    assert p[0].severity == compliance.SEV_INFO
    assert "48小时内" in p[0].excerpt
    assert r.blocked is False


# --- new_promise 只报硬时限（裁决 R4）------------------------------------
# 口径与 agent/promise.py 的 hard/soft 分级对齐。库内真实话术钉死三种情形。

def test_hard_deadline_promise_is_reported(conn):
    """S00141 的真实话术：带可核算 deadline，必须报——它会真的进承诺追踪。"""
    r = compliance.check_reply(
        conn, "S00141", "专业",
        "目前我已经为您申请了【优先加急】处理通道，财务同事会在1小时内完成开具"
        "并重新发送至您的预留邮箱")
    p = [i for i in r.issues if i.kind == compliance.KIND_NEW_PROMISE]
    assert len(p) == 1 and "1小时内" in p[0].excerpt


def test_soft_promise_only_is_not_reported(conn):
    """纯软词不报。

    promise.py 已裁决「软承诺只记录不预警，否则误报会淹没真信号」。实测 173 条
    话术里 62 条只命中软词——占 35%，让它们和「48小时内一定发出」挂同一枚徽章，
    客服会把徽章当背景噪声划过去，真信号一起被忽略。
    """
    r = compliance.check_reply(
        conn, "S00099", "专业",
        "我马上帮您查一下，第一时间同步给您，这边一定盯到底，保证给您一个交代。")
    assert [i for i in r.issues if i.kind == compliance.KIND_NEW_PROMISE] == []


def test_apology_for_past_breach_is_not_a_new_promise(conn):
    """S00001 的真实话术：对过往破约的道歉回指，不是新承诺。"""
    r = compliance.check_reply(
        conn, "S00001", "致歉",
        "我刚才紧急核查了您的换货进度，发现由于仓库调度原因，未能按承诺在48小时内"
        "发出，这是我们的严重失职")
    assert [i for i in r.issues if i.kind == compliance.KIND_NEW_PROMISE] == []


def test_past_marker_in_other_clause_does_not_suppress(conn):
    """S00115 的真实话术：前半句的「已经」说的是已完成动作，后半句仍是新承诺。

    过往回指判定必须在**小句**粒度上做——整句粒度会把这条真承诺一起吃掉。
    """
    r = compliance.check_reply(
        conn, "S00115", "致歉",
        "为了弥补这次延误，我已经向仓库主管申请了【特急通道】，您的补发包裹将在"
        "今天内优先打包发出，预计明天会有物流揽收记录")
    p = [i for i in r.issues if i.kind == compliance.KIND_NEW_PROMISE]
    assert len(p) == 1 and "今天内" in p[0].excerpt


def test_periodic_deadline_is_reported(conn):
    """L2 提示词点名禁「每 4 小时」，正则必须查得到——两处不能分叉（Minor #8）。"""
    r = compliance.check_reply(conn, "S00099", "专业", "我会每4小时监控一次物流进度。")
    p = [i for i in r.issues if i.kind == compliance.KIND_NEW_PROMISE]
    assert len(p) == 1 and "每4小时" in p[0].excerpt


def test_issue_carries_span_into_original_text(conn):
    """M3b 卡片④要靠 start/end 高亮片段。"""
    text = "您的订单我已加急标记，48小时内一定发出。"
    r = compliance.check_reply(conn, "S00099", "专业", text)
    i = r.issues[0]
    assert text[i.start:i.end] == i.excerpt == "48小时内"


def test_repeated_excerpt_yields_one_issue_at_first_position(conn):
    """同一片段多次出现只出一个 issue，位置记首次——否则一条话术会刷屏。"""
    text = "48小时内发出，48小时内一定到，48小时内没到您找我。"
    r = compliance.check_reply(conn, "S00099", "专业", text)
    p = [i for i in r.issues if i.kind == compliance.KIND_NEW_PROMISE]
    assert len(p) == 1
    assert p[0].start == text.index("48小时内")


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


_FORBIDDEN_MODULES = ("agent.llm", "openai", "dashscope", "anthropic", "httpx")


def _scan_imports(src: str) -> set[str]:
    """源码里所有 import 到的模块名（含 `from X import Y` 的 X 与 X.Y）。"""
    import ast

    found: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            found.add(base)
            for a in node.names:
                found.add(f"{base}.{a.name}" if base else a.name)
    return {m for m in found if m}


def _hits_forbidden(mods: set[str]) -> set[str]:
    return {m for m in mods
            for bad in _FORBIDDEN_MODULES
            if m == bad or m.startswith(bad + ".")}


def test_does_not_import_llm():
    """合规校验必须是确定性的，不得依赖模型。

    **结构性检查，不是字符串检查。** 原来写的是 `assert "agent.llm" not in src`，
    但本仓库到处用 `from agent import llm`（见 agent/pipeline.py），那个写法不含
    子串 "agent.llm"——全局约束里最硬的一条实际没被守住。改用 ast 遍历
    Import / ImportFrom 节点比对模块名。
    """
    import inspect

    mods = _scan_imports(inspect.getsource(compliance))
    assert _hits_forbidden(mods) == set(), \
        f"合规校验不得依赖模型相关模块，实际 import 了 {sorted(mods)}"


@pytest.mark.parametrize("snippet", [
    "import agent.llm",
    "from agent import llm",
    "from agent.llm import DashScopeClient",
    "import openai",
    "from openai import OpenAI",
])
def test_import_guard_catches_every_writing_style(snippet):
    """守门测试自己要被守住：五种写法一种都不能漏。

    `from agent import llm` 正是本仓库最常用的写法，也正是旧的字符串检查漏掉的那种。
    """
    assert _hits_forbidden(_scan_imports(snippet)), f"守门规则漏掉了写法：{snippet}"
