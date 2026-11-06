"""前后端事件合同（SSE）。

协议：`data: {"type": "...", "payload": {...}}\n\n`
"""

from __future__ import annotations

from typing import Any

# —— 事件类型常量 ——
SESSION_CREATED = "session.created"
LOADING_PROGRESS = "loading.progress"
LOADING_DONE = "loading.done"
JUDGE_THINKING = "judge.thinking"
JUDGE_INITIAL = "judge.initial"
PHASE_CHANGED = "phase.changed"
ERROR = "error"

ALL_TYPES = (
    SESSION_CREATED,
    LOADING_PROGRESS,
    LOADING_DONE,
    JUDGE_THINKING,
    JUDGE_INITIAL,
    PHASE_CHANGED,
    ERROR,
)


def event(type_: str, **payload: Any) -> dict[str, Any]:
    """构造一条事件。类型不做强校验，方便明天扩展答辩事件。"""
    return {"type": type_, "payload": payload}
