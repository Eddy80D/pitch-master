"""T2 自测：mock 驱动状态机走完全程，校验事件序列。"""

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


async def main() -> int:
    config.ensure_dirs()
    session = state_machine.store.create("https://github.com/octocat/Hello-World")

    received: list[dict] = []

    async def consume() -> None:
        async for ev in session.subscribe(0):
            if ev is not None:
                received.append(ev)

    consumer = asyncio.create_task(consume())

    await session.emit(events.SESSION_CREATED, session_id=session.id)
    await session.set_phase("loading")
    await session.emit(events.LOADING_PROGRESS, msg="mock 读取中…")
    await session.emit(events.LOADING_DONE, files_read=3, files_skipped=1)

    await session.set_phase("analyzing")

    await session.set_phase("judging")
    for judge_id in ("tech", "biz", "vision", "originality", "aesthetic"):
        await session.emit(events.JUDGE_THINKING, judge_id=judge_id)
        await session.emit(
            events.JUDGE_INITIAL,
            judge_id=judge_id,
            score=10,
            verdict="turn",
            rationale="mock 评语",
        )
    await session.set_phase("initial_done")
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
        events.JUDGE_THINKING,
        events.JUDGE_INITIAL,
        events.JUDGE_THINKING,
        events.JUDGE_INITIAL,
        events.JUDGE_THINKING,
        events.JUDGE_INITIAL,
        events.JUDGE_THINKING,
        events.JUDGE_INITIAL,
        events.PHASE_CHANGED,
    ]
    actual = [ev["type"] for ev in received]

    print("收到事件数：", len(actual))
    for i, ev in enumerate(received):
        print(f"  {i:02d} {ev['type']} {ev['payload']}")

    if actual != expected:
        print("❌ 事件序列与预期不一致")
        print("预期：", expected)
        print("实际：", actual)
        return 1

    snapshot = session.snapshot()
    if snapshot.phase != "initial_done":
        print("❌ 最终阶段错误：", snapshot.phase)
        return 1

    saved = config.SESSIONS_DIR / f"{session.id}.json"
    if not saved.exists():
        print("❌ 会话未落盘：", saved)
        return 1

    print("✅ T2 状态机与事件序列通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
