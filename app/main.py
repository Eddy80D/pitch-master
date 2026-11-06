"""FastAPI 入口：创建会话 → 后台跑流水线 → SSE 推事件。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, events, repo_loader, state_machine
from .agents import analyst, judges
from .schemas import JudgeVerdict

logger = logging.getLogger("pitchmaster")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_background_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.ensure_dirs()
    config.validate()
    logger.info("PitchMaster 启动，模型：%s / %s", config.MODEL_PRO, config.MODEL_FLASH)
    yield


app = FastAPI(title="路演大师 PitchMaster", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.ROOT / "static")), name="static")


class CreateSessionBody(BaseModel):
    repo_url: str = Field(min_length=3)
    # TODO(窦柘): PRD / 成品网址今晚只接收不参与评审，D2 接入证据包与答辩
    prd: str | None = None
    demo_url: str | None = None


def _validate_repo_url(repo_url: str) -> str:
    value = repo_url.strip()
    if not re.match(r"^(https?://(www\.)?github\.com/|[\w.-]+/[\w.-]+$)", value):
        raise HTTPException(
            status_code=400,
            detail="请填写 GitHub 仓库地址，例如 https://github.com/owner/repo",
        )
    return value


async def _run_pipeline(session: state_machine.Session) -> None:
    try:
        # 1) 抓仓库
        await session.set_phase("loading")
        await session.emit(events.LOADING_PROGRESS, msg="正在获取仓库…")
        loaded = await asyncio.to_thread(repo_loader.load_repository, session.repo_url)
        await session.emit(
            events.LOADING_PROGRESS,
            msg=f"已读取 {loaded['files_read']} 个文件，跳过 {loaded['files_skipped']} 个",
        )
        await session.emit(
            events.LOADING_DONE,
            files_read=loaded["files_read"],
            files_skipped=loaded["files_skipped"],
            files_total=loaded.get("files_total", 0),
            chars=len(loaded["text"]),
        )

        # 2) 解剖
        await session.set_phase("analyzing")
        pack = await analyst.analyze(loaded["text"], session.repo_url)
        pack.files_read = loaded["files_read"]
        pack.files_skipped = loaded["files_skipped"]
        session.evidence = pack

        # 3) 5 位评委并行初评
        await session.set_phase("judging")

        async def on_thinking(judge_id: str) -> None:
            await session.emit(events.JUDGE_THINKING, judge_id=judge_id)

        async def on_verdict(verdict: JudgeVerdict) -> None:
            await session.emit(
                events.JUDGE_INITIAL,
                judge_id=verdict.judge_id,
                name=verdict.name,
                dimension=verdict.dimension,
                weight=verdict.weight,
                score=verdict.score,
                verdict=verdict.verdict,
                rationale=verdict.rationale,
                questions=verdict.questions,
                degraded=verdict.degraded,
            )
            if verdict.degraded:
                await session.emit(
                    events.ERROR,
                    stage="judging",
                    msg=f"评委「{verdict.name}」掉线：{verdict.error}",
                    degraded=True,
                )

        verdicts = await judges.evaluate_all(pack, on_thinking, on_verdict)
        session.verdicts = verdicts
        await session.set_phase("initial_done")

    except Exception as exc:  # 流水线任何环节失败都要让前端看到
        logger.exception("会话 %s 失败", session.id)
        session.error = {"stage": session.phase, "msg": str(exc), "degraded": False}
        await session.emit(events.ERROR, stage=session.phase, msg=str(exc), degraded=False)
    finally:
        await session.close()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(config.ROOT / "static" / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "model_pro": config.MODEL_PRO,
        "model_flash": config.MODEL_FLASH,
        "base_url": config.MIMO_BASE_URL,
    }


@app.post("/api/sessions")
async def create_session(body: CreateSessionBody) -> dict[str, str]:
    repo_url = _validate_repo_url(body.repo_url)
    session = state_machine.store.create(repo_url, body.prd, body.demo_url)
    await session.emit(
        events.SESSION_CREATED,
        session_id=session.id,
        repo_url=session.repo_url,
    )
    task = asyncio.create_task(_run_pipeline(session))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"session_id": session.id}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    session = state_machine.store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session.snapshot().model_dump()


@app.get("/api/sessions/{session_id}/events")
async def stream_events(session_id: str, request: Request) -> StreamingResponse:
    session = state_machine.store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    start = 0
    last_event_id = request.headers.get("last-event-id")
    if last_event_id and last_event_id.isdigit():
        start = int(last_event_id) + 1

    async def generator() -> AsyncIterator[str]:
        index = start
        async for event in session.subscribe(start):
            if event is None:
                yield ": ping\n\n"
                continue
            yield f"id: {index}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            index += 1

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
