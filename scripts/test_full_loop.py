"""T16 mock 全流程：不调真实 API，验证闭环事件、transcript、报告落盘。

覆盖：intro → qa(4 问) → deliberation → bicker → reveal → report → done
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import config, events, main, state_machine  # noqa: E402
from app.agents import judges, report as report_agent  # noqa: E402
from app.schemas import EvidencePack, FinalVerdict, JudgeVerdict  # noqa: E402


def build_pack() -> EvidencePack:
    return EvidencePack(
        repo_url="https://github.com/demo/demo",
        summary="一个面向大学生的 AI 简历优化工具。",
        tech_stack=["FastAPI", "Next.js"],
        highlights=[
            {
                "point": "流式改写降低等待感",
                "evidence": [{"file": "app/rewrite.py", "lines": "42-88", "quote": "yield chunk"}],
            }
        ],
        concerns=[
            {
                "point": "缺少限流",
                "evidence": [{"file": "app/main.py", "lines": "15-40", "quote": "@app.post"}],
            }
        ],
        injection_alert={"detected": False, "location": "", "quote": ""},
        files_read=2,
        files_skipped=0,
    )


def build_initial() -> list[JudgeVerdict]:
    result: list[JudgeVerdict] = []
    for item in judges.load_judges():
        weight = int(item["weight"])
        score = 0 if weight == 0 else max(1, weight // 2)
        result.append(
            JudgeVerdict(
                judge_id=item["id"],
                name=item["name"],
                dimension=item["dimension"],
                weight=weight,
                score=score,
                verdict="hold",
                rationale="mock 初评",
                questions=["你的技术方案怎么落地？", "有没有验证数据？"],
            )
        )
    return result


def build_final(initial: list[JudgeVerdict]) -> list[FinalVerdict]:
    plan = {
        "tech": (20, "turn"),
        "biz": (8, "hold"),
        "vision": (25, "turn"),
        "originality": (5, "hold"),
        "aesthetic": (0, "turn"),
    }
    result: list[FinalVerdict] = []
    for verdict in initial:
        score, final_verdict = plan[verdict.judge_id]
        result.append(
            FinalVerdict(
                judge_id=verdict.judge_id,
                name=verdict.name,
                dimension=verdict.dimension,
                weight=verdict.weight,
                initial_score=verdict.score,
                final_score=score,
                direction="up" if score >= verdict.score else "down",
                reason="mock 复议：引用了用户原话",
                final_verdict=final_verdict,
            )
        )
    return result


async def submit_retry(session: state_machine.Session, kind: str, index: int, text: str) -> bool:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if await session.submit_input(kind, index, text):
            return True
        await asyncio.sleep(0.01)
    return False


async def run(mode: str = "normal") -> int:
    config.ensure_dirs()
    initial = build_initial()
    finals = build_final(initial)
    pack = build_pack()
    calls = {"intro": 0, "answer": 0, "reaction": 0, "reconsider": 0, "report": 0}

    def fake_load(repo_url: str) -> dict:
        return {
            "text": "mock repo",
            "files_read": 2,
            "files_skipped": 0,
            "files_total": 2,
            "repo_url": repo_url,
        }

    async def fake_analyze(
        text: str, repo_url: str, prd_text: str = "", demo_text: str = ""
    ) -> EvidencePack:
        return pack

    async def fake_evaluate_all(pack_, on_thinking=None, on_verdict=None):
        for verdict in initial:
            if on_thinking:
                await on_thinking(verdict.judge_id)
            if on_verdict:
                await on_verdict(verdict)
        return list(initial)

    async def fake_reaction(judge, question, answer):
        calls["reaction"] += 1
        return "回答具体，但还缺限流方案。"

    async def fake_reconsider(
        initial_, transcript_text, encore_ids=None, demo_provided=True
    ):
        calls["reconsider"] += 1
        assert "自我介绍" in transcript_text
        if mode != "skip":
            assert "回答" in transcript_text
        return list(finals)

    async def fake_bicker(finals_, transcript_text):
        yield {"judge_id": "vision", "name": "未来学家", "text": "然后呢？数据在哪？"}
        yield {"judge_id": "biz", "name": "商业老炮", "text": "所以，谁付钱？"}
        yield {"judge_id": "vision", "name": "未来学家", "text": "先交一单再谈未来。"}

    async def fake_retry_initial(judge_id, pack_):
        return None

    async def fake_report(data):
        calls["report"] += 1
        assert data["totals"]["valid_weight"] == 100
        assert data["totals"]["final"] == 58.0
        return "# 预评审报告 · mock\n\n## 总分与临场力\n\n初评 50 → 终评 58"

    main.repo_loader.load_repository = fake_load
    main.analyst.analyze = fake_analyze
    main.judges.evaluate_all = fake_evaluate_all
    main.judges.react = fake_reaction
    main.judges.reconsider_all = fake_reconsider
    main.judges.bicker_lines = fake_bicker
    main.judges.retry_initial = fake_retry_initial
    report_agent.generate = fake_report

    session = state_machine.store.create("https://github.com/demo/demo")
    task = asyncio.create_task(main._run_pipeline(session))

    seen: list[dict] = []
    async for ev in session.subscribe(0):
        seen.append(ev)
        if ev["type"] == events.INTRO_REQUESTED:
            if mode == "skip":
                assert await session.request_skip() is True
            else:
                assert await submit_retry(session, "intro", 0, "我是窦柘，做了一个 AI 简历工具。")
                calls["intro"] += 1
        elif ev["type"] == events.QA_QUESTION:
            index = ev["payload"]["index"]
            assert await submit_retry(session, "answer", index, f"第 {index} 问的回答：我们做了流式与限流。")
            calls["answer"] += 1

    await asyncio.wait_for(task, timeout=30)

    types = [ev["type"] for ev in seen]
    # P0-1：judging 阶段不得出现任何分数，只允许 judge.ready
    assert events.JUDGE_READY in types
    assert events.JUDGE_INITIAL not in types
    reveal_at = types.index(events.REVEAL_CHAIR)
    for ev in seen[:reveal_at]:
        assert "score" not in ev["payload"], ev
    assert events.INTRO_REQUESTED in types
    if mode == "skip":
        assert types.count(events.QA_QUESTION) == 0
        assert types.count(events.QA_REACTION) == 0
        assert any(
            item["role"] == "user_intro" and item["text"] == "[跳过]"
            for item in session.transcript
        )
    else:
        assert types.count(events.QA_QUESTION) == config.QA_MAX_QUESTIONS
        assert types.count(events.QA_REACTION) == config.QA_MAX_QUESTIONS
        roles = [item["role"] for item in session.transcript]
        assert roles.count("judge_q") == config.QA_MAX_QUESTIONS
        assert roles.count("user_a") == config.QA_MAX_QUESTIONS
        assert roles.count("judge_reaction") == config.QA_MAX_QUESTIONS
    assert types.count(events.REVEAL_CHAIR) == 5
    assert events.BICKER_LINE in types
    assert events.REPORT_READY in types
    assert session.phase == "done"

    roles = [item["role"] for item in session.transcript]
    assert roles.count("user_intro") == 1

    report_path = config.REPORTS_DIR / f"{session.id}.md"
    assert report_path.exists(), report_path
    assert "预评审报告" in report_path.read_text(encoding="utf-8")

    print(f"[{mode}] 事件总数：{len(types)}")
    print(f"[{mode}] 调用统计：{calls}")
    print(f"[{mode}] 阶段：{session.phase}｜transcript 条目：{len(session.transcript)}")
    print(f"[{mode}] 报告：{report_path}")
    print(f"✅ T16 mock 全流程通过（{mode}）")

    report_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    code = asyncio.run(run("normal"))
    if code == 0:
        code = asyncio.run(run("skip"))
    raise SystemExit(code)
