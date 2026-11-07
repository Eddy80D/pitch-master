"""D3.5 营业打磨自测：测试单分类、自动放行/核验、IP 并发护栏。不调真实 API。"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from fastapi import HTTPException  # noqa: E402

from app import config, main, orders, state_machine  # noqa: E402


class FakeClient:
    def __init__(self, host: str):
        self.host = host


class FakeRequest:
    def __init__(self, host: str):
        self.client = FakeClient(host)


async def fake_pipeline(session: state_machine.Session) -> None:
    await session.set_phase("loading")
    await session.set_phase("done")
    await session.close()


async def create(host: str, repo: str) -> tuple[dict, state_machine.Session]:
    result = await main.create_session(
        main.CreateSessionBody(repo_url=repo),
        FakeRequest(host),
    )
    return result, state_machine.store.get(result["session_id"])


async def run() -> int:
    original_pipeline = main._run_pipeline
    original = {
        key: getattr(config, key)
        for key in (
            "PRECHECK_ENABLED",
            "PAYMENT_REQUIRED",
            "AUTO_APPROVE",
            "ADMIN_TOKEN",
            "MAX_ACTIVE_SESSIONS_PER_IP",
            "ORDERS_FILE",
            "ORDERS_SUMMARY_FILE",
        )
    }

    tmpdir = Path(tempfile.mkdtemp(prefix="pm_polish_"))
    config.ORDERS_FILE = tmpdir / "orders.jsonl"
    config.ORDERS_SUMMARY_FILE = tmpdir / "orders_summary.json"
    config.PRECHECK_ENABLED = False
    config.PAYMENT_REQUIRED = True
    config.AUTO_APPROVE = True
    config.ADMIN_TOKEN = "test-token"
    config.MAX_ACTIVE_SESSIONS_PER_IP = 3

    orders.store._orders.clear()
    orders.store._loaded = True
    state_machine.store._sessions.clear()
    main._run_pipeline = fake_pipeline

    try:
        # ---------------- 自动放行 + 待核验
        _, session1 = await create("10.0.0.1", "https://github.com/demo/one")
        order1 = orders.store.get(session1.id)
        assert order1.is_test is False and order1.status == "unpaid"

        paid = await main.mark_paid(session1.id)
        assert paid["status"] == "approved"
        order1 = orders.store.get(session1.id)
        assert order1.status in {"approved", "running"}
        assert order1.auto_approved is True
        assert order1.verify_status == "unverified"

        stats = orders.store.stats()
        assert stats["unverified_count"] == 1
        assert stats["total_revenue"] == config.PRICE_YUAN
        await main.admin_verify(session1.id, token="test-token")
        assert orders.store.get(session1.id).verify_status == "verified"
        assert orders.store.stats()["unverified_count"] == 0
        print("✅ 付款即放行；待核验 → 已核验；自动放行计入营业额")

        # ---------------- 逃单标记
        _, session2 = await create("10.0.0.2", "https://github.com/demo/two")
        await main.mark_paid(session2.id)
        await main.admin_evade(session2.id, token="test-token")
        assert orders.store.get(session2.id).verify_status == "evaded"
        assert orders.store.stats()["evaded_count"] == 1
        print("✅ 逃单标记持久化")

        # ---------------- 测试单：PAYMENT_REQUIRED=False 自动标记
        config.PAYMENT_REQUIRED = False
        _, test_session = await create("10.0.0.3", "https://github.com/demo/test")
        test_order = orders.store.get(test_session.id)
        assert test_order.is_test is True
        assert test_order.status in {"approved", "running"}
        stats = orders.store.stats()
        assert stats["test_count"] == 1
        assert stats["total_revenue"] == round(config.PRICE_YUAN * 2, 2)
        assert orders.store.suggestions(
            test_order, [], {"final": 0, "valid_weight": 100, "delta": 0}
        ) == []
        print("✅ 自测单自动标记测试；不计营业额；跳过质检建议")

        # ---------------- 手动标记 / 取消测试
        config.PAYMENT_REQUIRED = True
        await main.admin_test_flag(
            session1.id,
            main.TestFlagBody(is_test=True),
            token="test-token",
        )
        stats = orders.store.stats()
        assert stats["test_count"] == 2
        assert stats["total_revenue"] == config.PRICE_YUAN
        await main.admin_test_flag(
            session1.id,
            main.TestFlagBody(is_test=False),
            token="test-token",
        )
        assert orders.store.stats()["total_revenue"] == round(
            config.PRICE_YUAN * 2, 2
        )
        print("✅ 手动标记/取消测试单，统计口径即时切换")

        # ---------------- 持久化回放
        reloaded = orders.OrderStore()
        reloaded.load()
        restored = reloaded.get(session1.id)
        assert restored is not None
        assert restored.auto_approved is True
        assert restored.verify_status == "verified"
        assert reloaded.get(test_session.id).is_test is True
        print("✅ 重启回放保留 is_test / auto_approved / verify_status")

        # ---------------- IP 并发护栏
        orders.store._orders.clear()
        state_machine.store._sessions.clear()
        config.PAYMENT_REQUIRED = True
        config.AUTO_APPROVE = False
        for index in range(3):
            await create("10.9.9.9", f"https://github.com/demo/ip{index}")
        try:
            await create("10.9.9.9", "https://github.com/demo/ip-over")
        except HTTPException as exc:
            assert exc.status_code == 429
            assert "排队" in exc.detail
        else:
            raise AssertionError("第 4 个并发会话未被拦截")
        print("✅ 同 IP 第 4 个活跃会话返回 429")

        print("✅ D3.5 营业打磨自测全部通过")
        return 0
    finally:
        main._run_pipeline = original_pipeline
        for key, value in original.items():
            setattr(config, key, value)
        orders.store._orders.clear()
        orders.store._loaded = False
        state_machine.store._sessions.clear()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
