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

# —— D2 答辩 / 报告 ——
# 演示模式为现场默认（Q6 裁决）；完整模式把三个值改成 10 / 120 / 180 即可
QA_MAX_QUESTIONS: int = int(os.getenv("QA_MAX_QUESTIONS", "4"))
QA_ANSWER_TIMEOUT_S: float = float(os.getenv("QA_ANSWER_TIMEOUT_S", "45"))
INTRO_TIMEOUT_S: float = float(os.getenv("INTRO_TIMEOUT_S", "30"))
# TODO(窦柘): 插话权只预留开关，v1 未接入答辩流程（D2 规格允许默认关闭）
INTERJECTION_ENABLED: bool = os.getenv("INTERJECTION_ENABLED", "False").lower() in {
    "1",
    "true",
    "yes",
}
REPORT_MODEL: str = os.getenv("REPORT_MODEL", MODEL_PRO)

# —— 支付 / 老板后台（P0-4）——
# PAYMENT_REQUIRED=False 时跳过收款直接跑，供开发与评审演示
PAYMENT_REQUIRED: bool = os.getenv("PAYMENT_REQUIRED", "True").lower() in {
    "1",
    "true",
    "yes",
}
# 为空则 admin 接口全部 404
ADMIN_TOKEN: str = os.getenv("ADMIN_TOKEN", "")

# —— D3 营业版 ——
# 先尝后买：提交后先零 token 预检，再收费；关掉则回到"提交即收款"
PRECHECK_ENABLED: bool = os.getenv("PRECHECK_ENABLED", "True").lower() in {
    "1",
    "true",
    "yes",
}
PRICE_YUAN: float = float(os.getenv("PRICE_YUAN", "19.9"))

LOG_DIR: Path = _path_env("LOG_DIR", ROOT / "logs")
USAGE_FILE: Path = LOG_DIR / "usage.jsonl"
SESSIONS_DIR: Path = LOG_DIR / "sessions"
ORDERS_FILE: Path = LOG_DIR / "orders.jsonl"
ORDERS_SUMMARY_FILE: Path = LOG_DIR / "orders_summary.json"
REPORTS_DIR: Path = _path_env("REPORTS_DIR", ROOT / "reports")

JUDGES_FILE: Path = ROOT / "judges.yaml"

# 仓库加载上限（规格 4.5）
MAX_FILE_CHARS: int = 32_000
MAX_TOTAL_CHARS: int = 300_000


def ensure_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def validate() -> None:
    """启动自检：缺 key 时给出明确中文报错。"""
    if not MIMO_API_KEY:
        raise RuntimeError("缺少 MIMO_API_KEY，请在项目根目录的 .env 中配置")
