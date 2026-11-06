"""5 位评委并行初评。人设全部来自 judges.yaml。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import yaml

from .. import config, mimo_client
from ..schemas import EvidencePack, JudgeVerdict, extract_json

JUDGE_PROMPT = """你是黑客松评审团的「{name}」，负责评估【{dimension}】（满分 {weight}）。
人设：{persona}
基于以下证据包（JSON），输出严格 JSON：
{{"score": 0-{weight}的整数, "verdict": "turn"|"hold", "rationale": "≤80字，毒舌但有建设性，必须引用证据", "questions": ["答辩要追问的问题1", "问题2"]}}
verdict=turn 表示你愿意为该项目转身（分数≥权重的70%）。证据包如下："""

ZERO_WEIGHT_NOTE = "\n注意：你只评价不计分，score 必须为 0；verdict 依你的专业判断给出 turn 或 hold。"

RETRY_PROMPT = "上面的输出无法解析为合法 JSON。请只输出约定字段的 JSON，不要 markdown 代码块，不要解释。"

ThinkingCallback = Callable[[str], Awaitable[None]] | None
VerdictCallback = Callable[[JudgeVerdict], Awaitable[None]] | None


def load_judges() -> list[dict[str, Any]]:
    raw = yaml.safe_load(config.JUDGES_FILE.read_text(encoding="utf-8"))
    judges = raw.get("judges") if isinstance(raw, dict) else None
    if not judges:
        raise ValueError("judges.yaml 里没有 judges 列表")
    return judges


def _pack_json(pack: EvidencePack) -> str:
    return json.dumps(pack.model_dump(), ensure_ascii=False, indent=2)


def _clamp(value: int, weight: int) -> int:
    if weight <= 0:
        return 0
    return max(0, min(weight, value))


async def _evaluate_one(
    judge: dict[str, Any],
    pack: EvidencePack,
    on_thinking: ThinkingCallback = None,
) -> JudgeVerdict:
    judge_id = judge["id"]
    name = judge.get("name", judge_id)
    dimension = judge.get("dimension", "")
    weight = int(judge.get("weight", 0))

    if on_thinking is not None:
        await on_thinking(judge_id)

    prompt = JUDGE_PROMPT.format(
        name=name,
        dimension=dimension,
        weight=weight,
        persona=judge.get("persona", ""),
    )
    if weight == 0:
        prompt += ZERO_WEIGHT_NOTE

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": _pack_json(pack)},
    ]

    last_error: Exception | None = None
    for attempt in range(2):
        text = await mimo_client.chat(
            f"judge:{judge_id}",
            config.MODEL_FLASH,
            messages,
            timeout=180,
            max_tokens=1200,
        )
        try:
            data = extract_json(text)
            verdict = JudgeVerdict(
                judge_id=judge_id,
                name=name,
                dimension=dimension,
                weight=weight,
                **{
                    key: data.get(key)
                    for key in ("score", "verdict", "rationale", "questions")
                    if data.get(key) is not None
                },
            )
            verdict.score = _clamp(verdict.score, weight)
            # 分数与 verdict 不一致时，以「≥权重70%」为准（评委常写反）
            if weight > 0 and "verdict" not in data:
                verdict.verdict = "turn" if verdict.score >= weight * 0.7 else "hold"
            return verdict
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                messages = messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": RETRY_PROMPT},
                ]

    raise ValueError(f"评委「{name}」输出无法解析：{last_error}")


def _degraded_verdict(judge: dict[str, Any], error: Exception) -> JudgeVerdict:
    return JudgeVerdict(
        judge_id=judge["id"],
        name=judge.get("name", judge["id"]),
        dimension=judge.get("dimension", ""),
        weight=int(judge.get("weight", 0)),
        score=0,
        verdict="hold",
        rationale="评委暂时掉线（安可机制待接入）",
        questions=[],
        degraded=True,
        error=str(error)[:300],
    )


async def evaluate_all(
    pack: EvidencePack,
    on_thinking: ThinkingCallback = None,
    on_verdict: VerdictCallback = None,
) -> list[JudgeVerdict]:
    """并行初评，单个评委失败不影响其他评委；每出一份就回调一次。"""
    judges = load_judges()

    async def run_one(judge: dict[str, Any]) -> JudgeVerdict:
        try:
            verdict = await _evaluate_one(judge, pack, on_thinking)
        except Exception as exc:
            verdict = _degraded_verdict(judge, exc)
        if on_verdict is not None:
            await on_verdict(verdict)
        return verdict

    return await asyncio.gather(*[run_one(judge) for judge in judges])
