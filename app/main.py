"""FastAPI 入口：支付放行 → 完整闭环 → SSE 推事件。

D2 补丁：
- P0-1 初评分数不下发，judging 只发 judge.ready；reveal 才亮终评
- P0-2 PRD / 成品网址接入分析
- P0-4 支付拦截 + 老板后台放行
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
        "PitchMaster 启动，模型：%s / %s，演示模式：%s 问 / %ss / 介绍 %ss，支付拦截：%s",
        config.MODEL_PRO,
        config.MODEL_FLASH,
        config.QA_MAX_QUESTIONS,
        config.QA_ANSWER_TIMEOUT_S,
        config.INTRO_TIMEOUT_S,
        "ON" if config.PAYMENT_REQUIRED else "OFF",
    )
    yield


app = FastAPI(title="路演大师 PitchMaster", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.ROOT / "static")), name="static")


class CreateSessionBody(BaseModel):
    repo_url: str = Field(min_length=3)
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


def _start_pipeline(session: state_machine.Session) -> None:
    if session.pipeline_started:
        return
    session.pipeline_started = True
    task = asyncio.create_task(_run_pipeline(session))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _build_report_data(session: state_machine.Session) -> dict[str, Any]:
    pack = session.evidence
    return {
        "repo_url": session.repo_url,
        "evidence": pack.model_dump() if pack else {},
        "prd_text": session.prd_text,
        "demo_text": session.demo_text,
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
        # ---------------------------------------------------------- 仓库 + 材料
        await session.set_phase("loading")
        await session.emit(events.LOADING_PROGRESS, msg="正在获取仓库…")
        loaded = await asyncio.to_thread(repo_loader.load_repository, session.repo_url)
        await session.emit(
            events.LOADING_PROGRESS,
            msg=f"已读取 {loaded['files_read']} 个文件，跳过 {loaded['files_skipped']} 个",
        )

        session.prd_text = (session.prd or "").strip()[:20_000]
        session.demo_text = ""
        if session.demo_url:
            await session.emit(events.LOADING_PROGRESS, msg="正在读取成品网址…")
            session.demo_text = await asyncio.to_thread(
                repo_loader.fetch_demo_page, session.demo_url
            )
            await session.emit(
                events.LOADING_PROGRESS,
                msg="成品网址已读取" if session.demo_text else "成品网址读取失败，按未提供处理",
            )

        await session.emit(
            events.LOADING_DONE,
            files_read=loaded["files_read"],
            files_skipped=loaded["files_skipped"],
            files_total=loaded.get("files_total", 0),
            chars=len(loaded["text"]),
            prd_provided=bool(session.prd_text),
            demo_provided=bool(session.demo_text),
        )

        # ---------------------------------------------------------- 解剖
        await session.set_phase("analyzing")
        pack = await analyst.analyze(
            loaded["text"],
            session.repo_url,
            session.prd_text,
            session.demo_text,
        )
        pack.files_read = loaded["files_read"]
        pack.files_skipped = loaded["files_skipped"]
        session.evidence = pack

        # ---------------------------------------------------------- 初评
        await session.set_phase("judging")

        async def on_thinking(judge_id: str) -> None:
            await session.emit(events.JUDGE_THINKING, judge_id=judge_id)

        async def on_verdict(verdict: JudgeVerdict) -> None:
            # P0-1：judging 阶段只报"已就位"，不下发任何分数
            await session.emit(
                events.JUDGE_READY,
                judge_id=verdict.judge_id,
                name=verdict.name,
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
            session,
            events.INTRO_REQUESTED,
            {"timeout_s": config.INTRO_TIMEOUT_S},
            "intro",
            0,
            config.INTRO_TIMEOUT_S,
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
                        "timeout_s": config.QA_ANSWER_TIMEOUT_S,
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
            session.verdict_initial,
            transcript_text,
            encore_ids,
            demo_provided=bool(session.demo_text),
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
                reason=item.reason,
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
        await session.emit(
            events.REPORT_READY,
            chars=len(report_text),
            totals=score_totals(session.verdict_initial, session.verdict_final),
            owner_comment=session.owner_comment,
        )

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


@app.get("/admin")
async def admin_page() -> FileResponse:
    return FileResponse(config.ROOT / "static" / "admin.html")


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
        "payment_required": config.PAYMENT_REQUIRED,
    }


@app.post("/api/sessions")
async def create_session(body: CreateSessionBody) -> dict[str, Any]:
    repo_url = _validate_repo_url(body.repo_url)
    session = state_machine.store.create(repo_url, body.prd, body.demo_url)
    await session.emit(
        events.SESSION_CREATED,
        session_id=session.id,
        repo_url=session.repo_url,
    )

    if config.PAYMENT_REQUIRED:
        await session.emit(
            events.PAYMENT_WAITING,
            qr_url="/static/pay_qr.png",
        )
        return {"session_id": session.id, "payment_required": True}

    session.paid_status = "approved"
    _start_pipeline(session)
    return {"session_id": session.id, "payment_required": False}


@app.post("/api/sessions/{session_id}/paid")
async def mark_paid(session_id: str) -> dict[str, str]:
    session = _get_session(session_id)
    if session.paid_status == "pending":
        return {"status": "pending"}
    if session.paid_status != "unpaid":
        raise HTTPException(status_code=409, detail="当前订单状态不能标记支付")
    session.paid_status = "pending"
    await session.emit(events.PAYMENT_PENDING)
    return {"status": "pending"}


def _require_admin(token: str) -> None:
    if not config.ADMIN_TOKEN or token != config.ADMIN_TOKEN:
        raise HTTPException(status_code=404, detail="Not Found")


@app.get("/api/admin/orders")
async def admin_orders(token: str = "") -> dict[str, Any]:
    _require_admin(token)
    orders = []
    for session in state_machine.store.all():
        if session.paid_status in {"unpaid", "pending"}:
            orders.append(
                {
                    "id": session.id,
                    "repo_url": session.repo_url,
                    "paid_status": session.paid_status,
                    "created_at": session.created_at,
                }
            )
    orders.sort(key=lambda item: item["created_at"])
    return {"orders": orders}


@app.post("/api/admin/approve/{session_id}")
async def admin_approve(session_id: str, token: str = "") -> dict[str, bool]:
    _require_admin(token)
    session = _get_session(session_id)
    if session.paid_status == "approved" or session.pipeline_started:
        return {"ok": True}
    if session.paid_status != "pending":
        raise HTTPException(status_code=409, detail="订单尚未标记支付")
    session.paid_status = "approved"
    await session.emit(events.PAYMENT_APPROVED)
    _start_pipeline(session)
    return {"ok": True}


@app.post("/api/admin/comment/{session_id}")
async def admin_comment(session_id: str, body: TextBody, token: str = "") -> dict[str, bool]:
    _require_admin(token)
    session = _get_session(session_id)
    session.owner_comment = body.text.strip()[:500]
    session._persist()
    return {"ok": True}


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
    last_event_id = request.headers.get("last-event-id")
    start = int(last_event_id) + 1 if last_event_id and last_event_id.isdigit() else 0

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
