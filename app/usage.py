"""usage.jsonl 聚合：每单 / 今日 / 本月 / 累计，按模型与成本换算。

pricing.yaml 为占位 0 时 cost_known=False，调用方必须显示"待官方价目确认"，
不得把 0 当作真实成本。
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path
from typing import Any

from . import config


def load_pricing() -> dict[str, Any]:
    """只解析 pricing.yaml 的简单结构，避免引入 YAML 依赖。"""
    data: dict[str, Any] = {}
    path: Path = config.PRICING_FILE
    if not path.exists():
        return data
    current: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and line.endswith(":"):
            current = line[:-1].strip()
            data[current] = {}
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        try:
            number = float(value.strip())
        except ValueError:
            continue
        if current and raw.startswith(" "):
            data[current][key] = number
        else:
            data[key] = number
            current = None
    return data


class UsageStore:
    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._mtime: float | None = None
        self._path: Path | None = None
        self._lock = threading.Lock()

    def load(self) -> list[dict[str, Any]]:
        path = config.USAGE_FILE
        if not path.exists():
            return []
        mtime = path.stat().st_mtime
        with self._lock:
            if mtime == self._mtime and path == self._path:
                return self._rows
            rows: list[dict[str, Any]] = []
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
            self._rows = rows
            self._mtime = mtime
            self._path = path
            return rows

    @staticmethod
    def _tokens(row: dict[str, Any]) -> int:
        total = row.get("total_tokens")
        if total is None:
            total = int(row.get("prompt_tokens") or 0) + int(
                row.get("completion_tokens") or 0
            )
        return int(total or 0)

    @staticmethod
    def _cost(rows: list[dict[str, Any]], pricing: dict[str, Any]) -> float:
        models = {
            key: value
            for key, value in pricing.items()
            if isinstance(value, dict) and ("input" in value or "output" in value)
        }
        total = 0.0
        for row in rows:
            prices = models.get(str(row.get("model") or ""), {})
            total += int(row.get("prompt_tokens") or 0) / 1_000_000 * float(
                prices.get("input", 0)
            )
            total += int(row.get("completion_tokens") or 0) / 1_000_000 * float(
                prices.get("output", 0)
            )
        return round(total, 6)

    def summary(self, now: float | None = None) -> dict[str, Any]:
        rows = self.load()
        pricing = load_pricing()
        models = {
            key: value
            for key, value in pricing.items()
            if isinstance(value, dict) and ("input" in value or "output" in value)
        }
        cost_known = any(
            float(value.get("input", 0)) or float(value.get("output", 0))
            for value in models.values()
        )

        now_dt = dt.datetime.fromtimestamp(now or dt.datetime.now().timestamp())
        today_start = dt.datetime.combine(now_dt.date(), dt.time.min).timestamp()
        month_start = dt.datetime(now_dt.year, now_dt.month, 1).timestamp()

        total_tokens = 0
        today_tokens = 0
        month_tokens = 0
        session_tokens: dict[str, int] = {}
        unattributed = 0
        by_model: dict[str, dict[str, Any]] = {}
        today_rows: list[dict[str, Any]] = []
        month_rows: list[dict[str, Any]] = []

        for row in rows:
            tokens = self._tokens(row)
            total_tokens += tokens
            ts = float(row.get("ts_epoch") or 0)
            if not ts:
                # 兼容历史记录：用 ts 字符串解析
                try:
                    ts = dt.datetime.strptime(
                        str(row.get("ts", "")), "%Y-%m-%dT%H:%M:%S%z"
                    ).timestamp()
                except ValueError:
                    ts = 0
            if ts >= today_start:
                today_tokens += tokens
                today_rows.append(row)
            if ts >= month_start:
                month_tokens += tokens
                month_rows.append(row)

            session_id = row.get("session_id")
            if session_id:
                session_tokens[session_id] = session_tokens.get(session_id, 0) + tokens
            else:
                unattributed += tokens

            model = str(row.get("model") or "unknown")
            bucket = by_model.setdefault(
                model, {"calls": 0, "tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
            )
            bucket["calls"] += 1
            bucket["tokens"] += tokens
            bucket["prompt_tokens"] += int(row.get("prompt_tokens") or 0)
            bucket["completion_tokens"] += int(row.get("completion_tokens") or 0)

        total_cost = self._cost(rows, pricing)
        today_cost = self._cost(today_rows, pricing) if cost_known else 0.0
        month_cost = self._cost(month_rows, pricing) if cost_known else 0.0

        return {
            "total_tokens": total_tokens,
            "today_tokens": today_tokens,
            "month_tokens": month_tokens,
            "unattributed_tokens": unattributed,
            "session_tokens": session_tokens,
            "by_model": by_model,
            "cost_known": cost_known,
            "total_cost": total_cost if cost_known else None,
            "today_cost": round(today_cost, 6) if cost_known else None,
            "month_cost": round(month_cost, 6) if cost_known else None,
        }

    def for_session(self, session_id: str) -> dict[str, Any]:
        rows = [row for row in self.load() if row.get("session_id") == session_id]
        pricing = load_pricing()
        models = {
            key: value
            for key, value in pricing.items()
            if isinstance(value, dict) and ("input" in value or "output" in value)
        }
        cost_known = any(
            float(value.get("input", 0)) or float(value.get("output", 0))
            for value in models.values()
        )
        return {
            "calls": len(rows),
            "tokens": sum(self._tokens(row) for row in rows),
            "cost_known": cost_known,
            "cost": self._cost(rows, pricing) if cost_known else None,
        }


store = UsageStore()
