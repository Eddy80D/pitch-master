"""T2/T9 自测：十阶段状态机、Future 等待点、超时、skip、事件序列。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import config, events, state_machine  # noqa: E402


def fresh() -> state_machine.Session:
    return state_machine.store.create("https://github.com/octocat/Hello-World")


async def test_timeout() -> None:
    session = fresh()
    await session.set_phase("intro")
    status, text = await session.wait_input("intro", 0, 0.05)
    assert (status, text) == ("timeout", None), (status, text)
    assert session.pending is None
    print("✅ 超时推进：0.05s 后返回 timeout，pending 已清空")


async def test_submit() -> None:
    session = fresh()
    await session.set_phase("intro")
    task = asyncio.create_task(session.wait_input("intro", 0, 2))
    await asyncio.sleep(0.02)
    assert await session.submit_input("intro", 0, "我是窦柘") is True
    status, text = await asyncio.wait_for(task, 2)
    assert (status, text) == ("ok", "我是窦柘"), (status, text)
    assert await session.submit_input("intro", 0, "重复提交") is False
    print("✅ Future 等待点：正常提交收到值，重复提交被拒")


async def test_skip() -> None:
    session = fresh()
    await session.set_phase("qa")
    task = asyncio.create_task(session.wait_input("answer", 1, 2))
    await asyncio.sleep(0.02)
    assert await session.request_skip() is True
    status, text = await asyncio.wait_for(task, 2)
    assert (status, text) == ("skip", None), (status, text)
    assert session.skip_requested is True
    await session.set_phase("deliberation")
    assert await session.request_skip() is False
    print("✅ skip：qa 阶段生效，deliberation 阶段返回 False")


async def test_sequence() -> None:
    config.ensure_dirs()
    session = fresh()
    received: list[dict] = []

    async def consume() -> None:
        async for ev in session.subscribe(0):
            if ev is not None:
                received.append(ev)

    consumer = asyncio.create_task(consume())

    await session.emit(events.SESSION_CREATED, session_id=session.id)
    await session.set_phase("loading")
    await session.emit(events.LOADING_PROGRESS, msg="mock")
    await session.emit(events.LOADING_DONE, files_read=1, files_skipped=0)
    await session.set_phase("analyzing")
    await session.set_phase("judging")
    await session.emit(events.JUDGE_THINKING, judge_id="tech")
    await session.emit(events.JUDGE_INITIAL, judge_id="tech", score=15, verdict="turn", rationale="mock")
    await session.set_phase("intro")
    await session.emit(events.INTRO_REQUESTED)
    await session.emit(events.INTRO_DONE, text="mock intro")
    await session.set_phase("qa")
    await session.emit(events.QA_START, total=1)
    await session.emit(events.QA_QUESTION, judge_id="tech", name="毒舌技术官", index=1, text="怎么实现？")
    await session.emit(events.QA_ANSWER, index=1, text="用的 FastAPI")
    await session.emit(events.QA_REACTION, judge_id="tech", name="毒舌技术官", text="还行")
    await session.emit(events.QA_END)
    await session.set_phase("deliberation")
    await session.emit(events.DELIBERATION_START)
    await session.emit(events.BICKER_LINE, judge_id="tech", name="毒舌技术官", text="这叫实现？")
    await session.set_phase("reveal")
    await session.emit(events.REVEAL_CHAIR, judge_id="tech", name="毒舌技术官", turned=True, final_score=18, weight=25)
    await session.set_phase("report")
    await session.emit(events.REPORT_READY, chars=100)
    await session.set_phase("done")
    await session.close()
    await asyncio.wait_for(consumer, timeout=5)

    expected = [
        events.SESSION_CREATED,
        events.PHASE_CHANGED,
        events.LOADING_PROGRESS,
        events.LOADING_DONE,
        events.PHASE_CHANGED,
        events.PHASE_CHANGED,
        events.JUDGE_THINKING,
        events.JUDGE_INITIAL,
        events.PHASE_CHANGED,
        events.INTRO_REQUESTED,
        events.INTRO_DONE,
        events.PHASE_CHANGED,
        events.QA_START,
        events.QA_QUESTION,
        events.QA_ANSWER,
        events.QA_REACTION,
        events.QA_END,
        events.PHASE_CHANGED,
        events.DELIBERATION_START,
        events.BICKER_LINE,
        events.PHASE_CHANGED,
        events.REVEAL_CHAIR,
        events.PHASE_CHANGED,
        events.REPORT_READY,
        events.PHASE_CHANGED,
    ]
    actual = [ev["type"] for ev in received]
    assert actual == expected, (actual, expected)

    saved = config.SESSIONS_DIR / f"{session.id}.json"
    assert saved.exists(), saved
    print(f"✅ 事件序列：{len(actual)} 条全部按序到达，会话已落盘")

    try:
        await session.set_phase("initial_done")
    except ValueError:
        print("✅ 阶段白名单：表外阶段 initial_done 被拒绝")
    else:
        raise AssertionError("表外阶段未被拒绝")


async def main() -> int:
    await test_timeout()
    await test_submit()
    await test_skip()
    await test_sequence()
    print("✅ T2/T9 状态机全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
