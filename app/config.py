"""集中配置：所有可变项从 .env 读取，代码里不出现任何密钥。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（app/ 的上一级）
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


def _path_env(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value) if value else default


MIMO_API_KEY: str = os.getenv("MIMO_API_KEY", "")
MIMO_BASE_URL: str = os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1").rstrip("/")
MODEL_PRO: str = os.getenv("MODEL_PRO", "mimo-v2.6-pro")
MODEL_FLASH: str = os.getenv("MODEL_FLASH", "mimo-v2.6-flash")
MIMO_TIMEOUT: float = float(os.getenv("MIMO_TIMEOUT", "180"))

LOG_DIR: Path = _path_env("LOG_DIR", ROOT / "logs")
USAGE_FILE: Path = LOG_DIR / "usage.jsonl"
SESSIONS_DIR: Path = LOG_DIR / "sessions"

JUDGES_FILE: Path = ROOT / "judges.yaml"

# 仓库加载上限（规格 4.5）
MAX_FILE_CHARS: int = 32_000
MAX_TOTAL_CHARS: int = 300_000


def ensure_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def validate() -> None:
    """启动自检：缺 key 时给出明确中文报错。"""
    if not MIMO_API_KEY:
        raise RuntimeError("缺少 MIMO_API_KEY，请在项目根目录的 .env 中配置")
