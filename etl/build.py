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
