"""D3-growth 自测：轻量注册、隐私边界、报告个性化、usage session_id、成本口径。"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import config, main, mimo_client, orders, state_machine, usage, users  # noqa: E402


def fake_request():
    return types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"))


async def fake_pipeline(session: state_machine.Session) -> None:
    await session.set_phase("done")
    await session.close()


async def run() -> int:
    original_pipeline = main._run_pipeline
    original = {
        key: getattr(config, key)
        for key in (
            "PRECHECK_ENABLED",
            "PAYMENT_REQUIRED",
            "AUTO_APPROVE",
            "ADMIN_TOKEN",
            "ORDERS_FILE",
            "ORDERS_SUMMARY_FILE",
            "USERS_FILE",
            "USAGE_FILE",
        )
    }
    tmpdir = Path(tempfile.mkdtemp(prefix="pm_growth_"))
    config.ORDERS_FILE = tmpdir / "orders.jsonl"
    config.ORDERS_SUMMARY_FILE = tmpdir / "orders_summary.json"
    config.USERS_FILE = tmpdir / "users.jsonl"
    config.USAGE_FILE = tmpdir / "usage.jsonl"
    config.PRECHECK_ENABLED = False
    config.PAYMENT_REQUIRED = False
    config.AUTO_APPROVE = True
    config.ADMIN_TOKEN = "test-token"

    orders.store._orders.clear()
    orders.store._loaded = True
    users.store._users.clear()
    users.store._loaded = True
    state_machine.store._sessions.clear()
    main._run_pipeline = fake_pipeline

    try:
        # ---------------- 注册 / 老用户识别
        first, is_new = users.store.register("窦柘", "wx_secret")
        assert is_new is True and first.session_count == 1
        second, is_new = users.store.register("窦柘", "")
        assert is_new is False and second.session_count == 2
        print("✅ 新称呼注册；同称呼第二次 → 次数 +1")

        # ---------------- 提交自动注册 + 欢迎回来
        result = await main.create_session(
            main.CreateSessionBody(
                repo_url="https://github.com/demo/growth",
                name="窦柘",
                contact="secret@example.com",
            ),
            fake_request(),
        )
        session = state_machine.store.get(result["session_id"])
        assert result["welcome_back"] is True
        assert result["session_count"] == 3
        assert session.user_name == "窦柘"
        assert session.user_session_count == 3
        assert orders.store.get(session.id).user_name == "窦柘"
        print("✅ 提交自动注册；第 3 次登台 welcome_back=True")

        # ---------------- 隐私边界：联系方式不出现在前端可见位置
        public = session.public_snapshot()
        events_text = json.dumps(session.events, ensure_ascii=False)
        report_data = main._build_report_data(session)
        assert "secret@example.com" not in json.dumps(public, ensure_ascii=False)
        assert "secret@example.com" not in events_text
        assert "secret@example.com" not in json.dumps(report_data, ensure_ascii=False)
        assert public["user_name"] == "窦柘"
        print("✅ 联系方式未进入公开快照 / SSE / 报告数据")

        # ---------------- 报告个性化
        personalized = main._personalize_report("# 预评审报告 · 原项目\n\n正文", "窦柘")
        assert personalized.startswith("# 预评审报告 · 窦柘")
        print("✅ 报告标题已按称呼个性化")

        # ---------------- usage 记 session_id + 四口径
        mimo_client.set_session_id(session.id)
        mimo_client._record_usage(
            "test",
            "mimo-v2.6-flash",
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )
        mimo_client.set_session_id(None)
        mimo_client._record_usage(
            "test-unattributed",
            "mimo-v2.6-pro",
            {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )

        usage_store = usage.UsageStore()
        summary = usage_store.summary()
        assert summary["total_tokens"] == 20
        assert summary["session_tokens"][session.id] == 15
        assert summary["unattributed_tokens"] == 5
        assert summary["cost_known"] is False
        assert summary["total_cost"] is None
        assert usage_store.for_session(session.id)["tokens"] == 15
        print("✅ usage 记录含 session_id；每单/累计/未归属口径正确")

        # ---------------- 订单详情含本单消耗
        detail = await main.admin_order_detail(session.id, token="test-token")
        assert detail["usage"]["tokens"] == 15
        assert detail["usage"]["calls"] == 1
        print("✅ 订单详情返回本单 token 消耗")

        # ---------------- 成本看板字段 + 用户列表
        stats = await main.admin_stats(token="test-token")
        assert stats["cost"]["total_tokens"] == 20
        assert stats["cost"]["cost_known"] is False
        assert stats["user_count"] == 1
        user_rows = await main.admin_users(token="test-token")
        assert user_rows["users"][0]["name"] == "窦柘"
        assert user_rows["users"][0]["contact"] == "secret@example.com"
        print("✅ stats 成本区块 + 注册用户数 + admin 用户列表（含联系方式）")

        # ---------------- 重启回放
        reloaded = users.UserStore()
        reloaded.load()
        assert reloaded.count() == 1
        assert reloaded.all()[0].session_count == 3
        print("✅ users.jsonl 重启回放正确")

        print("✅ D3-growth 自测全部通过")
        return 0
    finally:
        main._run_pipeline = original_pipeline
        for key, value in original.items():
            setattr(config, key, value)
        orders.store._orders.clear()
        orders.store._loaded = False
        users.store._users.clear()
        users.store._loaded = False
        state_machine.store._sessions.clear()
        usage.store._rows = []
        usage.store._mtime = None
        usage.store._path = None
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
