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
