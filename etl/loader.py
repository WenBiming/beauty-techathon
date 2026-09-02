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
