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

from . import config, events, orders, repo_loader, state_machine
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
    orders.store.load()
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


class QaBody(BaseModel):
    mark: str
    reason: str = ""


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


def _get_session_or_restore(session_id: str) -> state_machine.Session:
    """服务重启后，订单还在但 session 内存丢失：按订单元数据恢复一个会话。"""
    session = state_machine.store.get(session_id)
    if session is not None:
        return session
    order = orders.store.get(session_id)
    if order is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    session = state_machine.store.create(
        order.repo_url,
        order.prd,
        order.demo_url,
        session_id=order.order_id,
    )
    session.paid_status = order.status
    session.price = order.price
    session.owner_comment = order.owner_comment
    return session


def _start_pipeline(session: state_machine.Session) -> None:
    if session.pipeline_started:
        return
    session.pipeline_started = True
    orders.store.transition(session.id, "running")
    task = asyncio.create_task(_run_pipeline(session))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _finalize_order(session: state_machine.Session, success: bool) -> None:
    order = orders.store.get(session.id)
    if order is None:
        return
    if not success:
        orders.store.transition(session.id, "failed")
        return
    totals = score_totals(session.verdict_initial, session.verdict_final)
    reasons = orders.store.suggestions(order, session.verdict_final, totals)
    orders.store.transition(
        session.id,
        "done",
        totals=totals,
        qa_suggested=bool(reasons),
        qa_suggest_reasons=reasons,
    )


async def _run_precheck(session: state_machine.Session) -> None:
    """零 token 预检：先让用户看到"读到了什么"，再收费。"""
    try:
        await session.emit(
            events.LOADING_PROGRESS, msg="正在预检仓库（零 token，不调用模型）…"
        )
        loaded = await asyncio.to_thread(repo_loader.load_repository, session.repo_url)
    except Exception as exc:
        session.precheck_error = str(exc)
        orders.store.transition(session.id, "failed")
        await session.emit(
            events.ERROR, stage="precheck", msg=str(exc), degraded=False
        )
        await session.close()
        return

    session.repo_text = loaded["text"]
    session.precheck_done = True
    session.precheck_stats = {
        "repo_name": repo_loader.repo_name(session.repo_url),
        "files_read": loaded["files_read"],
        "files_skipped": loaded["files_skipped"],
        "files_total": loaded.get("files_total", 0),
        "chars": len(loaded["text"]),
        "suspicious_count": repo_loader.scan_suspicious(loaded["text"]),
        "price": session.price,
    }
    await session.emit(events.PRECHECK_DONE, **session.precheck_stats)

    if config.PAYMENT_REQUIRED:
        await session.emit(
            events.PAYMENT_WAITING,
            qr_url="/static/pay_qr.png",
            price=session.price,
        )
    else:
        orders.store.transition(session.id, "approved")
        _start_pipeline(session)


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
        if session.precheck_done:
            # 预检已完成：复用仓库文本，不再 clone
            await session.emit(
                events.LOADING_PROGRESS, msg="复用预检结果，开始解剖…"
            )
            loaded = {
                "text": session.repo_text,
                "files_read": session.precheck_stats.get("files_read", 0),
                "files_skipped": session.precheck_stats.get("files_skipped", 0),
                "files_total": session.precheck_stats.get("files_total", 0),
            }
        else:
            await session.set_phase("loading")
            await session.emit(events.LOADING_PROGRESS, msg="正在获取仓库…")
            loaded = await asyncio.to_thread(
                repo_loader.load_repository, session.repo_url
            )
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
                answer_reason = (
                    "skip"
                    if answer_status == "skip"
                    else "timeout"
                    if answer_status == "timeout"
                    else "user"
                )
                await session.emit(
                    events.QA_ANSWER,
                    index=position,
                    text=answer_text,
                    reason=answer_reason,
                )

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
        _finalize_order(session, True)

    except Exception as exc:  # 流水线任何环节失败都要让前端看到
        logger.exception("会话 %s 失败", session.id)
        session.error = {"stage": session.phase, "msg": str(exc), "degraded": False}
        _finalize_order(session, False)
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
        "precheck_enabled": config.PRECHECK_ENABLED,
        "price_yuan": config.PRICE_YUAN,
    }


@app.post("/api/sessions")
async def create_session(body: CreateSessionBody) -> dict[str, Any]:
    repo_url = _validate_repo_url(body.repo_url)
    session = state_machine.store.create(repo_url, body.prd, body.demo_url)
    session.price = config.PRICE_YUAN
    orders.store.create(
        session.id,
        repo_url,
        body.prd,
        body.demo_url,
        price=config.PRICE_YUAN,
    )
    await session.emit(
        events.SESSION_CREATED,
        session_id=session.id,
        repo_url=session.repo_url,
    )

    if config.PRECHECK_ENABLED:
        task = asyncio.create_task(_run_precheck(session))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return {
            "session_id": session.id,
            "payment_required": config.PAYMENT_REQUIRED,
            "precheck": True,
        }

    if config.PAYMENT_REQUIRED:
        await session.emit(
            events.PAYMENT_WAITING,
            qr_url="/static/pay_qr.png",
            price=session.price,
        )
        return {
            "session_id": session.id,
            "payment_required": True,
            "precheck": False,
        }

    session.paid_status = "approved"
    orders.store.transition(session.id, "approved")
    _start_pipeline(session)
    return {
        "session_id": session.id,
        "payment_required": False,
        "precheck": False,
    }


@app.post("/api/sessions/{session_id}/paid")
async def mark_paid(session_id: str) -> dict[str, str]:
    session = _get_session(session_id)
    if session.paid_status == "pending":
        return {"status": "pending"}
    if session.paid_status == "approved":
        raise HTTPException(status_code=409, detail="当前订单状态不能标记支付")
    if session.paid_status != "unpaid":
        return {"status": session.paid_status}
    session.paid_status = "pending"
    orders.store.transition(session.id, "pending")
    await session.emit(events.PAYMENT_PENDING)
    return {"status": "pending"}


def _require_admin(token: str) -> None:
    if not config.ADMIN_TOKEN or token != config.ADMIN_TOKEN:
        raise HTTPException(status_code=404, detail="Not Found")


@app.get("/api/admin/orders")
async def admin_orders(token: str = "") -> dict[str, Any]:
    _require_admin(token)
    rows = [order.model_dump() for order in orders.store.all()]
    rows.sort(key=lambda item: item["created_at"], reverse=True)
    return {"orders": rows[:200]}


@app.get("/api/admin/stats")
async def admin_stats(token: str = "") -> dict[str, Any]:
    _require_admin(token)
    return orders.store.stats()


@app.get("/api/admin/orders/{session_id}")
async def admin_order_detail(session_id: str, token: str = "") -> dict[str, Any]:
    _require_admin(token)
    order = orders.store.get(session_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")

    session = state_machine.store.get(session_id)
    if session is not None:
        session_data = session.snapshot().model_dump()
    else:
        path = config.SESSIONS_DIR / f"{session_id}.json"
        try:
            session_data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            session_data = {}
    return {"order": order.model_dump(), "session": session_data}


@app.post("/api/admin/approve/{session_id}")
async def admin_approve(session_id: str, token: str = "") -> dict[str, bool]:
    _require_admin(token)
    session = _get_session_or_restore(session_id)
    order = orders.store.get(session_id)
    if order is not None and order.status == "done":
        return {"ok": True}
    if order is not None and order.status in {"approved", "running"}:
        if not session.pipeline_started:
            _start_pipeline(session)
        return {"ok": True}
    if session.paid_status not in {"pending", "approved"}:
        raise HTTPException(status_code=409, detail="订单尚未标记支付")
    session.paid_status = "approved"
    orders.store.transition(session.id, "approved")
    await session.emit(events.PAYMENT_APPROVED)
    _start_pipeline(session)
    return {"ok": True}


@app.post("/api/admin/comment/{session_id}")
async def admin_comment(session_id: str, body: TextBody, token: str = "") -> dict[str, bool]:
    _require_admin(token)
    session = state_machine.store.get(session_id)
    if session is not None:
        session.owner_comment = body.text.strip()[:500]
        session._persist()
    orders.store.transition(
        session_id, None, owner_comment=body.text.strip()[:500]
    )
    return {"ok": True}


@app.post("/api/admin/qa/{session_id}")
async def admin_qa(session_id: str, body: QaBody, token: str = "") -> dict[str, bool]:
    _require_admin(token)
    order = orders.store.get(session_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    if body.mark not in {"pass", "rework"}:
        raise HTTPException(status_code=400, detail="mark 只能是 pass 或 rework")
    reason = body.reason.strip()[:500]
    if body.mark == "rework" and not reason:
        raise HTTPException(status_code=400, detail="打回必须填写原因")
    orders.store.transition(
        session_id,
        None,
        qa_mark=body.mark,
        rework_reason=reason if body.mark == "rework" else "",
    )
    return {"ok": True}


@app.post("/api/admin/refund/{session_id}")
async def admin_refund(session_id: str, token: str = "") -> dict[str, bool]:
    _require_admin(token)
    order = orders.store.get(session_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    orders.store.transition(session_id, None, refunded=True)
    return {"ok": True}


@app.post("/api/admin/rework/{session_id}")
async def admin_rework(session_id: str, token: str = "") -> dict[str, str]:
    _require_admin(token)
    original = orders.store.get(session_id)
    if original is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    if original.qa_mark != "rework":
        raise HTTPException(status_code=409, detail="订单尚未被打回")

    source = state_machine.store.get(session_id)
    new_session = state_machine.store.create(
        original.repo_url, original.prd, original.demo_url
    )
    new_session.price = 0.0
    new_session.paid_status = "approved"
    if source is not None:
        new_session.repo_text = source.repo_text
        new_session.precheck_done = source.precheck_done
        new_session.precheck_stats = dict(source.precheck_stats)
        new_session.prd_text = source.prd_text
        new_session.demo_text = source.demo_text

    orders.store.create(
        new_session.id,
        original.repo_url,
        original.prd,
        original.demo_url,
        price=0.0,
    )
    orders.store.transition(
        new_session.id,
        "approved",
        rework_of=session_id,
        rework_reason=original.rework_reason,
        note="重审单",
    )
    orders.store.transition(session_id, "reworked", qa_mark="rework")

    await new_session.emit(
        events.SESSION_CREATED,
        session_id=new_session.id,
        repo_url=new_session.repo_url,
    )
    _start_pipeline(new_session)
    return {"session_id": new_session.id}


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
    return _get_session(session_id).public_snapshot()


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
