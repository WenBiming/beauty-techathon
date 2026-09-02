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
