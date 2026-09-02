"""路径常量与环境变量加载。"""
import os
from pathlib import Path

from dotenv import load_dotenv as _load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "app.db"
MOCK_IMAGE_DIR = DATA_DIR / "mock_images"
ENV_PATH = PROJECT_ROOT / ".env"

_FALLBACK_XLSX = Path(
    "/Users/wenbiming/Documents/misc/AI-Assist/赛题 1：数据共情者-业务数据.xlsx"
)


def _resolve_xlsx() -> Path:
    """官方 xlsx 路径的三级回退，保证项目能随源码 zip 一起提交、断网可运行
    （设计文档 §3.1）：
    1. 环境变量 XINJI_XLSX
    2. PROJECT_ROOT / data/raw/业务数据.xlsx（若存在）
    3. 本机开发绝对路径（保底）
    """
    env_path = os.environ.get("XINJI_XLSX")
    if env_path:
        return Path(env_path)
    bundled = PROJECT_ROOT / "data/raw/业务数据.xlsx"
    if bundled.is_file():
        return bundled
    return _FALLBACK_XLSX


SOURCE_XLSX = _resolve_xlsx()


def load_env() -> None:
    """加载 .env 到进程环境变量。显式传路径，避免 dotenv 在 stdin 场景下的栈探测失败。"""
    _load_dotenv(ENV_PATH)
