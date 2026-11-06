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


# ---------------------------------------------------------------- 会话状态
class SessionState(BaseModel):
    id: str
    repo_url: str
    prd: str | None = None
    demo_url: str | None = None
    phase: str = "created"
    evidence: EvidencePack | None = None
    verdicts: list[JudgeVerdict] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    created_at: float = 0.0


# ---------------------------------------------------------------- 工具函数
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
