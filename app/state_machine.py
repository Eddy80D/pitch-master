"""7 阶段状态机 + 内存会话存储 + JSON 落盘备份 + 事件订阅。

阶段顺序（今晚只走到 judging 完成）：
created → loading → analyzing → judging → rebuttal → review → report
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncIterator

from . import config, events
from .schemas import EvidencePack, JudgeVerdict, SessionState

PHASES = ["created", "loading", "analyzing", "judging", "rebuttal", "review", "report"]


class Session:
    def __init__(self, repo_url: str, prd: str | None = None, demo_url: str | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.repo_url = repo_url
        self.prd = prd
        self.demo_url = demo_url
        self.phase = "created"
        self.events: list[dict[str, Any]] = []
        self.evidence: EvidencePack | None = None
        self.verdicts: list[JudgeVerdict] = []
        self.error: dict[str, Any] | None = None
        self.created_at = time.time()
        self.closed = False
        self.condition = asyncio.Condition()
        self._persisted_events = 0

    # ------------------------------------------------------------ 事件
    async def emit(self, type_: str, **payload: Any) -> None:
        ev = events.event(type_, **payload)
        async with self.condition:
            self.events.append(ev)
            self.condition.notify_all()
        self._persist()

    async def set_phase(self, phase: str) -> None:
        if phase not in PHASES:
            # 允许今晚的临时终态（如 initial_done），不阻止流程
            pass
        self.phase = phase
        await self.emit(events.PHASE_CHANGED, phase=phase)

    async def close(self) -> None:
        self.closed = True
        async with self.condition:
            self.condition.notify_all()
        self._persist()

    async def subscribe(self, start: int = 0) -> AsyncIterator[dict[str, Any] | None]:
        """从 start 位置重放，然后持续推送。yield None 表示心跳。"""
        idx = max(0, start)
        while True:
            timed_out = False
            async with self.condition:
                while idx >= len(self.events) and not self.closed:
                    try:
                        await asyncio.wait_for(self.condition.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        timed_out = True
                        break
                batch = self.events[idx :]
                idx += len(batch)

            for ev in batch:
                yield ev

            if self.closed and idx >= len(self.events):
                return
            if timed_out and not batch:
                yield None

    # ------------------------------------------------------------ 快照
    def snapshot(self) -> SessionState:
        return SessionState(
            id=self.id,
            repo_url=self.repo_url,
            prd=self.prd,
            demo_url=self.demo_url,
            phase=self.phase,
            evidence=self.evidence,
            verdicts=self.verdicts,
            error=self.error,
            created_at=self.created_at,
        )

    def _persist(self) -> None:
        """落盘备份。失败不影响主流程。"""
        try:
            config.ensure_dirs()
            payload = self.snapshot().model_dump()
            payload["events"] = self.events
            path = config.SESSIONS_DIR / f"{self.id}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create(self, repo_url: str, prd: str | None = None, demo_url: str | None = None) -> Session:
        session = Session(repo_url=repo_url, prd=prd, demo_url=demo_url)
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)


store = SessionStore()
