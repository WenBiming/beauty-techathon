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
