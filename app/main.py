"""FastAPI 入口：创建会话 → 后台跑完整闭环 → SSE 推事件。

D2 闭环：
仓库 → 解剖 → 初评 → 自我介绍 → 答辩 → 复议 → 互怼 → 亮椅 → 报告 → done
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, events, repo_loader, state_machine
from .agents import analyst, judges
from .agents import report as report_agent
from .schemas import JudgeVerdict, score_totals, transcript_to_text

logger = logging.getLogger("pitchmaster")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_background_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.ensure_dirs()
    config.validate()
    logger.info(
        "PitchMaster 启动，模型：%s / %s，演示模式：%s 问 / %ss / 介绍 %ss",
        config.MODEL_PRO,
        config.MODEL_FLASH,
        config.QA_MAX_QUESTIONS,
        config.QA_ANSWER_TIMEOUT_S,
        config.INTRO_TIMEOUT_S,
    )
    yield


app = FastAPI(title="路演大师 PitchMaster", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.ROOT / "static")), name="static")


class CreateSessionBody(BaseModel):
    repo_url: str = Field(min_length=3)
    # TODO(窦柘): PRD / 成品网址今晚只接收不参与评审，D2 之后接入证据包与答辩
    prd: str | None = None
    demo_url: str | None = None


class TextBody(BaseModel):
    text: str = ""


def _validate_repo_url(repo_url: str) -> str:
    value = repo_url.strip()
    if not re.match(r"^(https?://(www\.)?github\.com/|[\w.-]+/[\w.-]+$)", value):
        raise HTTPException(
            status_code=400,
            detail="请填写 GitHub 仓库地址，例如 https://github.com/owner/repo",
        )
    return value


def _get_session(session_id: str) -> state_machine.Session:
    session = state_machine.store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


def _build_report_data(session: state_machine.Session) -> dict[str, Any]:
    pack = session.evidence
    return {
        "repo_url": session.repo_url,
        "evidence": pack.model_dump() if pack else {},
        "initial": [verdict.model_dump() for verdict in session.verdict_initial],
        "final": [verdict.model_dump() for verdict in session.verdict_final],
        "transcript": session.transcript,
        "bicker": session.bicker,
        "totals": score_totals(session.verdict_initial, session.verdict_final),
    }


async def _wait_with_event(
    session: state_machine.Session,
    event_type: str,
    payload: dict[str, Any],
    kind: str,
    index: int,
    timeout_s: float,
) -> tuple[str, str | None]:
    """先挂上 Future，再发事件，避免用户抢在 pending 建立前提交。"""
    waiter = asyncio.create_task(session.wait_input(kind, index, timeout_s))
    await asyncio.sleep(0)
    await session.emit(event_type, **payload)
    return await waiter


async def _run_pipeline(session: state_machine.Session) -> None:
    try:
        # ---------------------------------------------------------- D1：初评
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

        await session.set_phase("analyzing")
        pack = await analyst.analyze(loaded["text"], session.repo_url)
        pack.files_read = loaded["files_read"]
        pack.files_skipped = loaded["files_skipped"]
        session.evidence = pack

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
        session.verdict_initial = verdicts

        # ---------------------------------------------------------- 自我介绍
        await session.set_phase("intro")
        status, intro_text = await _wait_with_event(
            session, events.INTRO_REQUESTED, {}, "intro", 0, config.INTRO_TIMEOUT_S
        )
        skip_all = status == "skip"
        if status == "ok":
            text = (intro_text or "").strip() or "（用户未填写内容）"
        elif status == "timeout":
            text = "用户未作介绍"
        else:
            text = "[跳过]"
        session.intro_text = text
        session.add_transcript("user_intro", text)
        await session.emit(events.INTRO_DONE, text=text)
        session.skip_requested = False

        # ---------------------------------------------------------- 答辩
        if not skip_all:
            await session.set_phase("qa")
            queue = judges.build_question_queue(
                session.verdict_initial, config.QA_MAX_QUESTIONS
            )
            await session.emit(events.QA_START, total=len(queue))

            for position, item in enumerate(queue, 1):
                if session.skip_requested:
                    break
                session.add_transcript(
                    "judge_q",
                    item["text"],
                    judge_id=item["judge_id"],
                    judge_name=item["name"],
                    index=position,
                )
                answer_status, answer = await _wait_with_event(
                    session,
                    events.QA_QUESTION,
                    {
                        "judge_id": item["judge_id"],
                        "name": item["name"],
                        "index": position,
                        "text": item["text"],
                    },
                    "answer",
                    position,
                    config.QA_ANSWER_TIMEOUT_S,
                )
                if answer_status == "skip":
                    answer_text = "[跳过]"
                elif answer_status == "timeout":
                    answer_text = "（超时未答）"
                else:
                    answer_text = (answer or "").strip() or "（空回答）"

                session.add_transcript(
                    "user_a",
                    answer_text,
                    judge_id=item["judge_id"],
                    judge_name=item["name"],
                    index=position,
                )
                await session.emit(events.QA_ANSWER, index=position, text=answer_text)

                if answer_status == "skip":
                    for rest_position, rest in enumerate(
                        queue[position:], position + 1
                    ):
                        session.add_transcript(
                            "user_a",
                            "[跳过]",
                            judge_id=rest["judge_id"],
                            judge_name=rest["name"],
                            index=rest_position,
                        )
                    break

                judge_meta = judges.judge_map().get(item["judge_id"], {})
                reaction = await judges.react(judge_meta, item["text"], answer_text)
                session.add_transcript(
                    "judge_reaction",
                    reaction,
                    judge_id=item["judge_id"],
                    judge_name=item["name"],
                    index=position,
                )
                await session.emit(
                    events.QA_REACTION,
                    judge_id=item["judge_id"],
                    name=item["name"],
                    text=reaction,
                )

            await session.emit(events.QA_END)
        else:
            # intro 阶段点了"直接宣判"：答辩记 0 问，直接合议
            await session.emit(events.QA_START, total=0)
            await session.emit(events.QA_END)

        session.skip_requested = False

        # ---------------------------------------------------------- 复议
        await session.set_phase("deliberation")
        await session.emit(events.DELIBERATION_START)

        encore_ids: set[str] = set()
        if session.evidence is not None:
            for index, verdict in enumerate(session.verdict_initial):
                if not verdict.degraded:
                    continue
                retried = await judges.retry_initial(verdict.judge_id, session.evidence)
                if retried is not None and not retried.degraded:
                    session.verdict_initial[index] = retried
                    encore_ids.add(retried.judge_id)
                    await session.emit(
                        events.ENCORE,
                        judge_id=retried.judge_id,
                        name=retried.name,
                    )

        transcript_text = transcript_to_text(session.transcript)
        finals = await judges.reconsider_all(
            session.verdict_initial, transcript_text, encore_ids
        )
        session.verdict_final = finals

        # ---------------------------------------------------------- 互怼
        async for line in judges.bicker_lines(finals, transcript_text):
            session.bicker.append(line)
            await session.emit(events.BICKER_LINE, **line)

        # ---------------------------------------------------------- 亮椅
        await session.set_phase("reveal")
        for item in sorted(finals, key=lambda verdict: verdict.weight):
            await asyncio.sleep(0.8)
            turned = None if item.degraded else item.final_verdict == "turn"
            final_score = None if item.degraded else item.final_score
            await session.emit(
                events.REVEAL_CHAIR,
                judge_id=item.judge_id,
                name=item.name,
                turned=turned,
                final_score=final_score,
                weight=item.weight,
            )

        # ---------------------------------------------------------- 报告
        await session.set_phase("report")
        report_data = _build_report_data(session)
        try:
            report_text = await report_agent.generate(report_data)
        except Exception as exc:
            logger.warning("报告生成失败，走模板降级：%s", exc)
            report_text = report_agent.fallback(report_data)

        session.report_text = report_text
        config.ensure_dirs()
        (config.REPORTS_DIR / f"{session.id}.md").write_text(
            report_text, encoding="utf-8"
        )
        session.report_ready = True
        await session.emit(events.REPORT_READY, chars=len(report_text))

        await session.set_phase("done")

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
        "qa_max_questions": config.QA_MAX_QUESTIONS,
        "qa_answer_timeout_s": config.QA_ANSWER_TIMEOUT_S,
        "intro_timeout_s": config.INTRO_TIMEOUT_S,
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


@app.post("/api/sessions/{session_id}/intro")
async def submit_intro(session_id: str, body: TextBody) -> dict[str, bool]:
    session = _get_session(session_id)
    if session.phase != "intro":
        raise HTTPException(status_code=409, detail="当前不在自我介绍阶段")
    text = body.text.strip()
    if len(text) > 500:
        raise HTTPException(status_code=400, detail="自我介绍不能超过 500 字")
    if not await session.submit_input("intro", 0, text):
        raise HTTPException(status_code=409, detail="本次自我介绍已结束或重复提交")
    return {"ok": True}


@app.post("/api/sessions/{session_id}/answer")
async def submit_answer(session_id: str, body: TextBody) -> dict[str, bool]:
    session = _get_session(session_id)
    if session.phase != "qa":
        raise HTTPException(status_code=409, detail="当前不在答辩阶段")
    pending = session.pending
    if not pending or pending.get("kind") != "answer":
        raise HTTPException(status_code=409, detail="当前没有待回答的问题")
    text = body.text.strip()
    if len(text) > 2000:
        raise HTTPException(status_code=400, detail="回答不能超过 2000 字")
    if not await session.submit_input("answer", pending["index"], text):
        raise HTTPException(status_code=409, detail="该问题已结束或重复提交")
    return {"ok": True}


@app.post("/api/sessions/{session_id}/skip")
async def skip_session(session_id: str) -> dict[str, bool]:
    session = _get_session(session_id)
    if not await session.request_skip():
        raise HTTPException(status_code=409, detail="当前阶段不能跳过")
    return {"ok": True}


@app.get("/api/sessions/{session_id}/report")
async def get_report(session_id: str):
    session = _get_session(session_id)
    if session.report_text:
        return PlainTextResponse(
            session.report_text,
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="pitchmaster-{session_id}.md"'
            },
        )
    if session.phase in {"deliberation", "reveal", "report"}:
        return JSONResponse(status_code=202, content={"status": "generating"})
    raise HTTPException(status_code=404, detail="报告尚未生成")


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    return _get_session(session_id).snapshot().model_dump()


@app.get("/api/sessions/{session_id}/events")
async def stream_events(session_id: str, request: Request) -> StreamingResponse:
    session = _get_session(session_id)

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
