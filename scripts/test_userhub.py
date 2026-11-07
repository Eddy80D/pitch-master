"""D3-userhub 自测：用户中心 overview、联系方式访问控制、报告数据、四转徽章。不改真实数据。"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from fastapi import HTTPException  # noqa: E402

from app import config, main, orders, prd, state_machine, users  # noqa: E402

WEIGHTS = [("tech", "比特", "完成度", 25), ("biz", "商业大亨", "商业化", 25),
           ("vision", "远见博士", "创新性", 35), ("originality", "摩斯", "原创性", 15)]


def verdicts(mode: str):
    initial = []
    final = []
    for judge_id, name, dimension, weight in WEIGHTS:
        initial.append({
            "judge_id": judge_id, "name": name, "dimension": dimension,
            "weight": weight, "score": 10, "verdict": "hold", "rationale": "mock",
        })
        final.append({
            "judge_id": judge_id, "name": name, "dimension": dimension,
            "weight": weight, "initial_score": 10,
            "final_score": 15 if mode == "four" else 10,
            "direction": "up", "reason": "mock",
            "final_verdict": "turn" if mode == "four" else "hold",
        })
    return initial, final


def write_session(session_id: str, user_name: str, mode: str, totals: dict, markdown: str):
    initial, final = verdicts(mode)
    data = {
        "id": session_id,
        "repo_url": "https://github.com/demo/project",
        "phase": "done",
        "report_ready": True,
        "user_name": user_name,
        "verdict_initial": initial,
        "verdict_final": final,
        "totals": totals,
        "transcript": [
            {"role": "user_intro", "text": "我们是测试队", "judge_id": None, "judge_name": None, "index": None},
            {"role": "judge_q", "text": "怎么实现？", "judge_id": "tech", "judge_name": "比特", "index": 1},
            {"role": "user_a", "text": "FastAPI", "judge_id": "tech", "judge_name": "比特", "index": 1},
        ],
        "bicker": [],
        "owner_comment": "继续打磨",
    }
    (config.SESSIONS_DIR / f"{session_id}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (config.REPORTS_DIR / f"{session_id}.md").write_text(markdown, encoding="utf-8")


async def run() -> int:
    original = {
        key: getattr(config, key)
        for key in (
            "ORDERS_FILE", "ORDERS_SUMMARY_FILE", "USERS_FILE",
            "SESSIONS_DIR", "REPORTS_DIR", "USAGE_FILE",
        )
    }
    tmpdir = Path(tempfile.mkdtemp(prefix="pm_hub_"))
    config.ORDERS_FILE = tmpdir / "orders.jsonl"
    config.ORDERS_SUMMARY_FILE = tmpdir / "orders_summary.json"
    config.USERS_FILE = tmpdir / "users.jsonl"
    config.SESSIONS_DIR = tmpdir / "sessions"
    config.REPORTS_DIR = tmpdir / "reports"
    config.SESSIONS_DIR.mkdir(parents=True)
    config.REPORTS_DIR.mkdir(parents=True)

    orders.store._orders.clear()
    orders.store._loaded = True
    users.store._users.clear()
    users.store._loaded = True
    state_machine.store._sessions.clear()

    try:
        # 正式注册 / 登录 / 重名 / PRD 提取
        reg = await main.auth_register(main.AuthBody(name="新用户", contact="wx_new"))
        assert reg["session_count"] == 0
        try:
            await main.auth_register(main.AuthBody(name="新用户", contact="wx_new"))
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("重名未拦截")
        try:
            await main.auth_login(main.AuthBody(name="新用户", contact="wrong"))
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("错联系方式登录未 404")
        login = await main.auth_login(main.AuthBody(name="新用户", contact="wx_new"))
        assert login["name"] == "新用户"
        assert prd.extract("prd.md", "# PRD\n\n微信登录".encode("utf-8")) == "# PRD\n\n微信登录"
        users.store._users.pop(next(k for k, v in users.store._users.items() if v.name == "新用户"))
        print("✅ 注册/重名/登录/PRD 提取通过")

        user1, _ = users.store.register("窦柘", "wx123")
        user2, _ = users.store.register("老用户", "")
        assert user1.contact == "wx123" and user2.contact == ""

        now = time.time()
        for order_id, user, mode, score, done_at, is_test in [
            ("s1", user1, "mixed", 62, now - 100, False),
            ("s2", user1, "four", 71, now - 50, False),
            ("s3", user1, "four", 99, now - 20, True),
        ]:
            metrics = {"initial": 55, "final": score, "delta": score - 55, "valid_weight": 100, "missing": []}
            orders.store.create(
                order_id, "https://github.com/demo/project",
                price=19.9, user_id=user.user_id, user_name=user.name, is_test=is_test,
            )
            orders.store.transition(
                order_id, "approved", approved_at=done_at, done_at=done_at,
            )
            orders.store.transition(order_id, "done", totals=metrics, done_at=done_at)
            write_session(order_id, user.name, mode, metrics, f"# 预评审报告 · {user.name}\n\n## 总分与临场力\n\n终评 {score}\n\n## 对质与证据附录\n\n`README:1` Hello\n")

        # 老订单没有 user_id，只有 user_name，向后兼容
        orders.store.create(
            "s4", "https://github.com/demo/legacy", price=19.9,
            user_id="", user_name="老用户",
        )
        legacy_totals = {"initial": 40, "final": 50, "delta": 10, "valid_weight": 100, "missing": []}
        orders.store.transition("s4", "approved", approved_at=now - 10, done_at=now - 10)
        orders.store.transition("s4", "done", totals=legacy_totals, done_at=now - 10)
        write_session("s4", "老用户", "mixed", legacy_totals, "# 预评审报告 · 老用户\n\n正文")

        # 联系方式必须匹配
        try:
            await main.user_overview("窦柘", contact="wrong")
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("错联系方式未返回 404")

        overview = await main.user_overview("窦柘", contact="wx123")
        assert overview["user"]["session_count"] == 1
        assert len(overview["orders"]) == 2, overview["orders"]  # 测试单被排除
        assert [item["score"] for item in overview["scores"]] == [62, 71]
        assert overview["badge_count"] == 1  # 只有 s2 四转
        print("✅ 用户中心：联系方式校验、测试单排除、分数曲线、四转徽章")

        legacy = await main.user_overview("老用户")
        assert len(legacy["orders"]) == 1 and legacy["scores"][0]["score"] == 50
        print("✅ 老数据仅凭称呼可查（联系方式为空，向后兼容）")

        report = await main.report_data("s2")
        assert report["markdown"].startswith("# 预评审报告 · 窦柘")
        assert report["totals"]["final"] == 71
        assert report["final"][0]["final_score"] == 15
        assert report["transcript"][0]["role"] == "user_intro"
        print("✅ 报告数据端点：markdown / totals / verdicts / transcript 齐全")

        print("✅ D3-userhub 自测全部通过")
        return 0
    finally:
        for key, value in original.items():
            setattr(config, key, value)
        orders.store._orders.clear()
        orders.store._loaded = False
        users.store._users.clear()
        users.store._loaded = False
        state_machine.store._sessions.clear()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
