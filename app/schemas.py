"""数据合同：证据包 / 评委结论 / 会话状态，以及模型输出的 JSON 解析。"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------- 证据相关
class EvidenceItem(BaseModel):
    file: str = ""
    lines: str = ""
    quote: str = ""


class PointItem(BaseModel):
    """亮点或疑点：一句结论 + 若干代码证据。"""

    point: str
    evidence: list[EvidenceItem] = Field(default_factory=list)

    @field_validator("evidence", mode="before")
    @classmethod
    def _normalize_evidence(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, dict):
            return [value]
        return value


class EvidencePack(BaseModel):
    repo_url: str
    summary: str = ""
    tech_stack: list[str] = Field(default_factory=list)
    highlights: list[PointItem] = Field(default_factory=list)
    concerns: list[PointItem] = Field(default_factory=list)
    injection_alert: dict[str, Any] = Field(
        default_factory=lambda: {"detected": False, "location": "", "quote": ""}
    )
    files_read: int = 0
    files_skipped: int = 0
    # P0-2：PRD / 成品网址是否提供（影响评委判断与 tech 分数上限）
    prd_provided: bool = False
    demo_provided: bool = False

    @field_validator("tech_stack", mode="before")
    @classmethod
    def _normalize_stack(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(v) for v in value]

    @field_validator("highlights", "concerns", mode="before")
    @classmethod
    def _normalize_points(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [{"point": value, "evidence": []}]
        out: list[Any] = []
        for item in value:
            if isinstance(item, str):
                out.append({"point": item, "evidence": []})
            elif isinstance(item, dict) and "point" not in item:
                # 容错：模型偶尔用 "highlight"/"concern"/"description" 作为键
                text = item.get("highlight") or item.get("concern") or item.get("description") or ""
                out.append({"point": str(text), "evidence": item.get("evidence") or []})
            else:
                out.append(item)
        return out

    @field_validator("injection_alert", mode="before")
    @classmethod
    def _normalize_injection(cls, value: Any) -> Any:
        if isinstance(value, bool):
            return {"detected": value, "location": "", "quote": ""}
        if not isinstance(value, dict):
            return {"detected": False, "location": "", "quote": ""}
        return {
            "detected": bool(value.get("detected", False)),
            "location": str(value.get("location") or ""),
            "quote": str(value.get("quote") or ""),
        }


# ---------------------------------------------------------------- 评委相关
class JudgeVerdict(BaseModel):
    judge_id: str
    name: str = ""
    dimension: str = ""
    weight: int = 0
    score: int = 0
    verdict: str = "hold"
    rationale: str = ""
    questions: list[str] = Field(default_factory=list)
    degraded: bool = False
    error: str = ""

    @field_validator("questions", mode="before")
    @classmethod
    def _normalize_questions(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(v) for v in value]

    @field_validator("score", mode="before")
    @classmethod
    def _to_int(cls, value: Any) -> Any:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalize_verdict(cls, value: Any) -> str:
        return "turn" if str(value).strip().lower() in {"turn", "yes", "accept"} else "hold"


class FinalVerdict(BaseModel):
    """复议后的终评结论。"""

    judge_id: str
    name: str = ""
    dimension: str = ""
    weight: int = 0
    initial_score: int = 0
    final_score: int = 0
    direction: str = "hold"
    reason: str = ""
    final_verdict: str = "hold"
    encore: bool = False
    degraded: bool = False

    @field_validator("initial_score", "final_score", mode="before")
    @classmethod
    def _to_int(cls, value: Any) -> Any:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @field_validator("direction", mode="before")
    @classmethod
    def _normalize_direction(cls, value: Any) -> str:
        value = str(value).strip().lower()
        return value if value in {"up", "down", "hold"} else "hold"

    @field_validator("final_verdict", mode="before")
    @classmethod
    def _normalize_final_verdict(cls, value: Any) -> str:
        return "turn" if str(value).strip().lower() in {"turn", "yes", "accept"} else "hold"


# ---------------------------------------------------------------- 会话状态
class SessionState(BaseModel):
    id: str
    repo_url: str
    prd: str | None = None
    demo_url: str | None = None
    phase: str = "created"
    evidence: EvidencePack | None = None
    verdicts: list[JudgeVerdict] = Field(default_factory=list)
    verdict_initial: list[JudgeVerdict] = Field(default_factory=list)
    verdict_final: list[FinalVerdict] = Field(default_factory=list)
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    intro_text: str | None = None
    bicker: list[dict[str, Any]] = Field(default_factory=list)
    report_ready: bool = False
    paid_status: str = "unpaid"
    owner_comment: str = ""
    totals: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    created_at: float = 0.0


# ---------------------------------------------------------------- 工具函数
def transcript_to_text(transcript: list[dict[str, Any]]) -> str:
    """把结构化 transcript 转成评委可读的文本。"""
    lines: list[str] = []
    for item in transcript:
        role = item.get("role")
        text = str(item.get("text") or "")
        if role == "user_intro":
            lines.append(f"[自我介绍] {text}")
        elif role == "judge_q":
            lines.append(f"[第{item.get('index', '?')}问 · {item.get('judge_name', '评委')}] {text}")
        elif role == "user_a":
            lines.append(f"[回答] {text}")
        elif role == "judge_reaction":
            lines.append(f"[{item.get('judge_name', '评委')}反应] {text}")
    return "\n".join(lines) if lines else "（无答辩记录）"


def score_totals(
    initial: list[JudgeVerdict],
    final: list[FinalVerdict],
) -> dict[str, Any]:
    """总分口径（Q5）：Σfinal / Σ(有效维度 weight) × 100，缺席维度排除分母。"""
    initial_map = {item.judge_id: item for item in initial}
    valid = [item for item in final if item.weight > 0 and not item.degraded]
    valid_weight = sum(item.weight for item in valid)
    final_sum = sum(item.final_score for item in valid)
    def _initial_score(item: FinalVerdict) -> int:
        original = initial_map.get(item.judge_id)
        return original.score if original is not None else item.initial_score

    initial_sum = sum(_initial_score(item) for item in valid)

    def _pct(value: int) -> float:
        return round(value / valid_weight * 100, 1) if valid_weight else 0.0

    final_pct = _pct(final_sum)
    initial_pct = _pct(initial_sum)
    return {
        "initial": initial_pct,
        "final": final_pct,
        "delta": round(final_pct - initial_pct, 1),
        "valid_weight": valid_weight,
        "missing": [item.name for item in final if item.weight > 0 and item.degraded],
    }


_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


def extract_json(text: str) -> dict[str, Any]:
    """从模型回复里抠出第一个完整 JSON 对象。

    兼容：markdown 代码块、前后废话、尾随逗号。
    """
    if not text:
        raise ValueError("模型返回为空")

    cleaned = _FENCE_RE.sub("", text.strip())

    # 先尝试整体解析
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # 再扫描括号配对，取第一个平衡的 {...}
    start = cleaned.find("{")
    if start == -1:
        raise ValueError("模型返回中找不到 JSON 对象")

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = cleaned[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    # 常见毛病：尾随逗号
                    fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
                    return json.loads(fixed)

    raise ValueError("模型返回的 JSON 不完整")
