"""D2 十阶段状态机 + 内存会话存储 + JSON 落盘备份 + 事件订阅。

阶段顺序：
created → loading → analyzing → judging → intro → qa
        → deliberation → reveal → report → done

按 Kimi Q2 裁决：等待点使用**一次性 Future**，不复用 Event。
按 Q3 裁决：所有状态转换由同一把 asyncio.Lock 保护，迟到输入返回 False（路由层转 409）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncIterator

from . import config, events
from .schemas import (
    EvidencePack,
    FinalVerdict,
    JudgeVerdict,
    SessionState,
    score_totals,
)

PHASES = [
    "created",
    "loading",
    "analyzing",
    "judging",
    "intro",
    "qa",
    "deliberation",
    "reveal",
    "report",
    "done",
]

# 内部哨兵：用户点击"直接宣判"
SKIP = object()


class Session:
    def __init__(self, repo_url: str, prd: str | None = None, demo_url: str | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.repo_url = repo_url
        self.prd = prd
        self.demo_url = demo_url
        self.phase = "created"
        self.events: list[dict[str, Any]] = []
        self.evidence: EvidencePack | None = None

        # D1：初评
        self.verdicts: list[JudgeVerdict] = []
        # D2：初评基线 / 终评 / 答辩记录 / 互怼 / 报告
        self.verdict_initial: list[JudgeVerdict] = []
        self.verdict_final: list[FinalVerdict] = []
        self.transcript: list[dict[str, Any]] = []
        self.intro_text: str | None = None
        self.bicker: list[dict[str, Any]] = []
        self.report_text: str | None = None
        self.report_ready = False
        self.prd_text: str = ""
        self.demo_text: str = ""

        # D3：预检（零 token）
        self.precheck_done = False
        self.precheck_stats: dict[str, Any] = {}
        self.precheck_error: str = ""
        self.repo_text: str = ""
        self.price: float = config.PRICE_YUAN

        # D3-growth：轻量用户（联系方式不进入 session / 快照）
        self.user_id: str = ""
        self.user_name: str = ""
        self.user_session_count: int = 0
        self.last_score: float | None = None

        # P0-4：支付与老板后台
        self.paid_status = "unpaid"  # unpaid → pending → approved
        self.owner_comment = ""
        self.pipeline_started = False

        self.error: dict[str, Any] | None = None
        self.created_at = time.time()
        self.closed = False

        # 等待点：{"kind": "intro"|"answer", "index": int, "future": Future}
        self.pending: dict[str, Any] | None = None
        self.skip_requested = False
        self.state_lock = asyncio.Lock()
        self.condition = asyncio.Condition()

    # ------------------------------------------------------------ 事件
    async def emit(self, type_: str, **payload: Any) -> None:
        ev = events.event(type_, **payload)
        async with self.condition:
            self.events.append(ev)
            self.condition.notify_all()
        self._persist()

    async def set_phase(self, phase: str) -> None:
        if phase not in PHASES:
            raise ValueError(f"非法阶段：{phase}")
        self.phase = phase
        await self.emit(events.PHASE_CHANGED, phase=phase)

    async def close(self) -> None:
        self.closed = True
        async with self.condition:
            self.condition.notify_all()
        self._persist()

    # ------------------------------------------------------------ 用户输入
    def add_transcript(
        self,
        role: str,
        text: str,
        *,
        judge_id: str | None = None,
        judge_name: str | None = None,
        index: int | None = None,
    ) -> None:
        self.transcript.append(
            {
                "role": role,
                "text": text,
                "judge_id": judge_id,
                "judge_name": judge_name,
                "index": index,
            }
        )

    async def wait_input(
        self, kind: str, index: int, timeout_s: float
    ) -> tuple[str, str | None]:
        """等待一次用户输入。

        返回 (status, text)：status ∈ {"ok", "timeout", "skip"}。
        超时后不再接受该次输入（pending 会被清空）。
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()

        async with self.state_lock:
            self.pending = {"kind": kind, "index": index, "future": future}

        try:
            value = await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
            if value is SKIP:
                return ("skip", None)
            return ("ok", str(value))
        except asyncio.TimeoutError:
            # 超时与提交同时发生的极小窗口：提交赢了就把值收下
            if future.done() and not future.cancelled():
                value = future.result()
                if value is SKIP:
                    return ("skip", None)
                return ("ok", str(value))
            return ("timeout", None)
        finally:
            async with self.state_lock:
                if self.pending is not None and self.pending.get("future") is future:
                    self.pending = None

    async def submit_input(self, kind: str, index: int, text: str) -> bool:
        """提交输入；kind/index 不匹配或已结束则返回 False。"""
        async with self.state_lock:
            pending = self.pending
            if not pending or pending["kind"] != kind or pending["index"] != index:
                return False
            future = pending["future"]
            if future.done():
                return False
            future.set_result(text)
            return True

    async def request_skip(self) -> bool:
        """跳过剩余答辩直达合议；仅 intro / qa 阶段有效（Q7）。"""
        async with self.state_lock:
            if self.phase not in ("intro", "qa"):
                return False
            self.skip_requested = True
            pending = self.pending
            if pending and not pending["future"].done():
                pending["future"].set_result(SKIP)
            return True

    # ------------------------------------------------------------ 订阅
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
            verdict_initial=self.verdict_initial,
            verdict_final=self.verdict_final,
            transcript=self.transcript,
            intro_text=self.intro_text,
            bicker=self.bicker,
            report_ready=self.report_ready,
            paid_status=self.paid_status,
            owner_comment=self.owner_comment,
            precheck_done=self.precheck_done,
            precheck_stats=self.precheck_stats,
            price=self.price,
            user_name=self.user_name,
            user_session_count=self.user_session_count,
            last_score=self.last_score,
            totals=(
                score_totals(self.verdict_initial, self.verdict_final)
                if self.verdict_final
                else {}
            ),
            error=self.error,
            created_at=self.created_at,
        )

    def public_snapshot(self) -> dict[str, Any]:
        """对前端公开的快照：不含任何初评/终评明细，防止 F12 提前看分。"""
        data = self.snapshot().model_dump()
        data.pop("verdicts", None)
        data.pop("verdict_initial", None)
        data.pop("verdict_final", None)
        if self.phase not in {"report", "done"}:
            data["totals"] = {}
            data["owner_comment"] = ""
        return data

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

    def create(
        self,
        repo_url: str,
        prd: str | None = None,
        demo_url: str | None = None,
        session_id: str | None = None,
    ) -> Session:
        session = Session(repo_url=repo_url, prd=prd, demo_url=demo_url)
        if session_id:
            session.id = session_id
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def all(self) -> list[Session]:
        return list(self._sessions.values())


store = SessionStore()
