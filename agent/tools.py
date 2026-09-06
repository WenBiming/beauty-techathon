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
