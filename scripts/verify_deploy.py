"""发布验收脚本：python scripts/verify_deploy.py https://<domain>

检查：
1. /api/health 200 且包含 payment_required
2. 首页包含「评委天团」与注册入口
3. 创建会话（octocat）→ SSE 先 precheck.done 再 payment.waiting，且不自动推进
4. SSE 事件逐条到达
5. admin 错 token 返回 404

注意：B1 解剖前置后，本脚本会消耗 1 次 analyst 调用（不会触发评委/报告）。
"""

from __future__ import annotations

import json
import sys
import time

import httpx

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

REPO = "https://github.com/octocat/Hello-World"


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    return ok


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python scripts/verify_deploy.py https://<domain>")
        return 2
    base = sys.argv[1].rstrip("/")
    results: list[bool] = []

    try:
        health = httpx.get(f"{base}/api/health", timeout=20)
        data = health.json() if health.headers.get("content-type", "").startswith("application/json") else {}
        results.append(
            check(
                "health 200 + payment_required",
                health.status_code == 200 and "payment_required" in data,
                str(data)[:160],
            )
        )

        home = httpx.get(f"{base}/", timeout=20)
        text = home.text
        results.append(
            check(
                "首页含「评委天团」与注册入口",
                home.status_code == 200 and "评委天团" in text and "auth-register" in text,
            )
        )

        create = httpx.post(
            f"{base}/api/sessions",
            json={
                "repo_url": REPO,
                "name": f"发布验收{int(time.time()) % 100000}",
                "contact": "deploy-verify",
            },
            timeout=30,
        )
        if create.status_code != 200:
            results.append(check("创建会话", False, create.text[:200]))
            return 1
        session_id = create.json()["session_id"]
        print(f"       session = {session_id}")

        events: list[dict] = []
        started = time.monotonic()
        with httpx.stream(
            "GET",
            f"{base}/api/sessions/{session_id}/events",
            timeout=240,
        ) as response:
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[5:].strip())
                events.append(event)
                print(f"       +{time.monotonic() - started:5.1f}s {event['type']}")
                if event["type"] == "payment.waiting":
                    break

        types = [event["type"] for event in events]
        results.append(check("收到 precheck.done", "precheck.done" in types))
        results.append(check("收到 payment.waiting", "payment.waiting" in types))
        if "precheck.done" in types and "payment.waiting" in types:
            results.append(
                check(
                    "顺序正确：precheck.done → payment.waiting",
                    types.index("precheck.done") < types.index("payment.waiting"),
                )
            )
        results.append(
            check(
                "未人工放行时流水线不自动推进",
                not any(t in {"judge.thinking", "intro.requested", "qa.start"} for t in types),
            )
        )
        results.append(check("SSE 逐条事件（非空）", len(events) >= 3, f"{len(events)} 条"))

        wrong = httpx.get(f"{base}/api/admin/orders?token=definitely-wrong", timeout=20)
        results.append(check("admin 错 token 返回 404", wrong.status_code == 404, str(wrong.status_code)))
    except Exception as exc:
        results.append(check("请求过程", False, str(exc)))

    print("\n" + ("✅ ALL PASS" if all(results) else "❌ 存在 FAIL"))
    print("提示：验收产生的订单请用 admin 标为测试（is_test=true）。")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
