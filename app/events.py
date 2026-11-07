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
JUDGE_READY = "judge.ready"
JUDGE_INITIAL = "judge.initial"  # 仅保留常量；judging 阶段不再下发
PHASE_CHANGED = "phase.changed"
ERROR = "error"

# —— 支付 / 老板后台 ——
PAYMENT_WAITING = "payment.waiting"
PAYMENT_PENDING = "payment.pending"
PAYMENT_APPROVED = "payment.approved"

# —— D2 闭环 ——
INTRO_REQUESTED = "intro.requested"
INTRO_DONE = "intro.done"
QA_START = "qa.start"
QA_QUESTION = "qa.question"
QA_ANSWER = "qa.answer"
QA_REACTION = "qa.reaction"
QA_END = "qa.end"
DELIBERATION_START = "deliberation.start"
ENCORE = "encore"
BICKER_LINE = "bicker.line"
REVEAL_CHAIR = "reveal.chair"
REPORT_READY = "report.ready"

ALL_TYPES = (
    SESSION_CREATED,
    LOADING_PROGRESS,
    LOADING_DONE,
    JUDGE_THINKING,
    JUDGE_READY,
    JUDGE_INITIAL,
    PHASE_CHANGED,
    ERROR,
    PAYMENT_WAITING,
    PAYMENT_PENDING,
    PAYMENT_APPROVED,
    INTRO_REQUESTED,
    INTRO_DONE,
    QA_START,
    QA_QUESTION,
    QA_ANSWER,
    QA_REACTION,
    QA_END,
    DELIBERATION_START,
    ENCORE,
    BICKER_LINE,
    REVEAL_CHAIR,
    REPORT_READY,
)


def event(type_: str, **payload: Any) -> dict[str, Any]:
    """构造一条事件。类型不做强校验，方便明天扩展答辩事件。"""
    return {"type": type_, "payload": payload}
