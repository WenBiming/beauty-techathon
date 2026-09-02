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
