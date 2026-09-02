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
