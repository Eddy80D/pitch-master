"""D2 补丁自测：P0-1 隐私 / P0-2 PRD 与成品 / P0-4 支付与老板后台。

不调真实 API。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path


def fake_request():
    return types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"))

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from fastapi import HTTPException  # noqa: E402

from app import config, main, orders, state_machine  # noqa: E402
from app.agents import judges  # noqa: E402
from app.repo_loader import _VisibleTextParser, _is_private_host  # noqa: E402
from app.schemas import EvidencePack  # noqa: E402


def test_demo_html_parser() -> None:
    parser = _VisibleTextParser()
    parser.feed(
        "<html><head><title>HEAD_ONLY</title></head><body>"
        "<h1>可见标题</h1><script>alert(1)</script><p>正文内容</p>"
        "<style>.x{color:red}</style></body></html>"
    )
    text = " ".join(parser.parts)
    assert "可见标题" in text and "正文内容" in text
    assert "alert" not in text and "color:red" not in text and "HEAD_ONLY" not in text
    assert _is_private_host("127.0.0.1")
    assert _is_private_host("localhost")
    assert _is_private_host("192.168.1.5")
    assert not _is_private_host("example.com")
    print("✅ P0-2 HTML 可见文本提取 / 内网地址拦截")


async def test_tech_cap() -> None:
    async def fake_chat(caller, model, messages, **kwargs):
        return json.dumps(
            {
                "score": 25,
                "verdict": "turn",
                "rationale": "mock",
                "questions": [],
            }
        )

    original = judges.mimo_client.chat
    judges.mimo_client.chat = fake_chat
    try:
        judge = judges.judge_map()["tech"]
        no_demo = EvidencePack(repo_url="https://github.com/a/b", demo_provided=False)
        verdict = await judges._evaluate_one(judge, no_demo)
        assert verdict.score <= 15, verdict.score
        assert verdict.verdict == "hold", verdict.verdict

        with_demo = EvidencePack(repo_url="https://github.com/a/b", demo_provided=True)
        verdict2 = await judges._evaluate_one(judge, with_demo)
        assert verdict2.score == 25, verdict2.score
    finally:
        judges.mimo_client.chat = original
    print("✅ P0-2 无成品网址时 tech 分数上限 15，有成品时正常")


async def wait_done(session: state_machine.Session, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if session.phase == "done":
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"会话未在 {timeout}s 内完成，phase={session.phase}")


def test_public_snapshot_has_no_scores() -> None:
    session = state_machine.store.create("https://github.com/demo/privacy")
    session.verdict_initial = [
        judges.JudgeVerdict(
            judge_id="tech",
            name="毒舌技术官",
            dimension="完成度",
            weight=25,
            score=20,
            verdict="turn",
            rationale="初评秘密",
        )
    ]
    session.phase = "judging"
    public = session.public_snapshot()
    assert "verdict_initial" not in public
    assert "verdicts" not in public
    assert "verdict_final" not in public
    assert public["totals"] == {}
    assert public["owner_comment"] == ""

    session.verdict_final = [
        judges.FinalVerdict(
            judge_id="tech",
            name="毒舌技术官",
            dimension="完成度",
            weight=25,
            initial_score=20,
            final_score=18,
            direction="down",
            reason="终评",
            final_verdict="turn",
        )
    ]
    session.phase = "done"
    public_done = session.public_snapshot()
    assert public_done["totals"]["valid_weight"] == 25
    assert "verdict_final" not in public_done
    print("✅ P0-1 公开快照不含初评/终评明细，仅 report/done 暴露总分")


async def test_payment_and_admin() -> None:
    original_pipeline = main._run_pipeline
    original_required = config.PAYMENT_REQUIRED
    original_token = config.ADMIN_TOKEN
    original_precheck = config.PRECHECK_ENABLED
    original_auto_approve = config.AUTO_APPROVE
    original_orders_file = config.ORDERS_FILE
    original_summary_file = config.ORDERS_SUMMARY_FILE

    tmpdir = Path(tempfile.mkdtemp(prefix="pm_orders_"))
    config.ORDERS_FILE = tmpdir / "orders.jsonl"
    config.ORDERS_SUMMARY_FILE = tmpdir / "orders_summary.json"
    orders.store._orders.clear()
    orders.store._loaded = True

    async def fake_pipeline(session):
        await session.set_phase("loading")
        await session.set_phase("done")
        await session.close()

    main._run_pipeline = fake_pipeline
    config.PAYMENT_REQUIRED = True
    config.PRECHECK_ENABLED = False
    config.AUTO_APPROVE = False
    config.ADMIN_TOKEN = "test-token"
    state_machine.store._sessions.clear()

    try:
        result = await main.create_session(
            main.CreateSessionBody(repo_url="https://github.com/demo/demo"),
            fake_request(),
        )
        session_id = result["session_id"]
        session = state_machine.store.get(session_id)
        assert result["payment_required"] is True
        assert session.pipeline_started is False
        assert session.paid_status == "unpaid"
        assert any(ev["type"] == "payment.waiting" for ev in session.events)

        # 错 token 一律 404
        try:
            await main.admin_orders(token="wrong")
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("错 token 未返回 404")

        await main.mark_paid(session_id)
        assert session.paid_status == "pending"
        order_list = await main.admin_orders(token="test-token")
        assert any(item["order_id"] == session_id for item in order_list["orders"])

        try:
            await main.admin_approve(session_id, token="wrong")
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("错 token 放行未返回 404")

        await main.admin_approve(session_id, token="test-token")
        assert session.paid_status == "approved"
        await wait_done(session)

        try:
            await main.admin_comment(session_id, main.TextBody(text="不错"), token="wrong")
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("错 token 评语未返回 404")

        await main.admin_comment(session_id, main.TextBody(text="老板亲签：继续打磨。"), token="test-token")
        assert session.owner_comment == "老板亲签：继续打磨。"
        print("✅ P0-4 支付拦截 / admin 404 / 放行启动 / 评语保存")

        # PAYMENT_REQUIRED=False 直接启动
        config.PAYMENT_REQUIRED = False
        result2 = await main.create_session(
            main.CreateSessionBody(repo_url="https://github.com/demo/demo2"),
            fake_request(),
        )
        assert result2["payment_required"] is False
        session2 = state_machine.store.get(result2["session_id"])
        await wait_done(session2)
        print("✅ P0-4 PAYMENT_REQUIRED=False 跳过收款直接启动")
    finally:
        main._run_pipeline = original_pipeline
        config.PAYMENT_REQUIRED = original_required
        config.PRECHECK_ENABLED = original_precheck
        config.AUTO_APPROVE = original_auto_approve
        config.ADMIN_TOKEN = original_token
        config.ORDERS_FILE = original_orders_file
        config.ORDERS_SUMMARY_FILE = original_summary_file
        orders.store._orders.clear()
        orders.store._loaded = False
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


async def main_test() -> int:
    test_demo_html_parser()
    await test_tech_cap()
    test_public_snapshot_has_no_scores()
    await test_payment_and_admin()
    print("✅ D2 补丁自测全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_test()))
