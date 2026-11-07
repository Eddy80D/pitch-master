"""T16 浏览器端到端：支付 → 初评 → 自我介绍 → 答辩 → 复议 → 亮椅 → 报告 → 刷新恢复。

用法：python scripts/test_e2e.py [base_url] [repo_url]
前置：服务已启动；本机有 Chrome（playwright 使用 channel=chrome）。
注意：会调用真实 MiMo API（约 16–20 次），请控制运行次数。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from playwright.sync_api import sync_playwright  # noqa: E402

from app import config  # noqa: E402


def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8123"
    repo_url = sys.argv[2] if len(sys.argv) > 2 else "https://github.com/octocat/Hello-World"

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on(
            "console",
            lambda msg: errors.append(f"console: {msg.text}")
            if msg.type == "error"
            else None,
        )

        def failed() -> bool:
            return page.locator("#dot.fail").count() > 0

        def dump_failure() -> None:
            print("[失败] 流水线报错，日志：")
            print(page.inner_text("#log"))
            page.screenshot(path=str(ROOT / "e2e_failure.png"), full_page=True)

        page.add_init_script(
            "sessionStorage.setItem('pm_user', JSON.stringify({name:'窦柘',contact:'test@example.com'}))"
        )
        page.goto(base_url, wait_until="domcontentloaded")
        page.fill("#repo", repo_url)
        page.fill("#user-name", "窦柘")
        page.fill("#contact", "test@example.com")
        page.click("#form-submit")
        page.wait_for_function(
            "new URLSearchParams(location.search).get('s')", timeout=30_000
        )
        e2e_session_id = page.evaluate(
            "new URLSearchParams(location.search).get('s')"
        )

        # 0) 支付拦截（PAYMENT_REQUIRED=True 时）
        page.wait_for_function(
            """() => {
                const pay = document.querySelector('#pay-box');
                return !pay.classList.contains('hidden')
                    || document.querySelectorAll('.judge.ready').length === 5
                    || document.querySelector('#dot').classList.contains('fail');
            }""",
            timeout=60_000,
        )
        if failed():
            dump_failure()
            browser.close()
            return 1

        if not page.locator("#pay-box").is_hidden():
            sid = page.evaluate("new URLSearchParams(location.search).get('s')")
            print("[支付] 拦截生效，session =", sid)
            page.click("#paid-btn")
            if not config.ADMIN_TOKEN:
                print("❌ 未配置 ADMIN_TOKEN")
                browser.close()
                return 1
            approve_url = f"{base_url}/api/admin/approve/{sid}?token={config.ADMIN_TOKEN}"
            for _ in range(10):
                resp = page.request.post(approve_url)
                if resp.ok:
                    break
                page.wait_for_timeout(300)
            else:
                print("❌ 老板放行失败")
                browser.close()
                return 1

            # 顺手留一条老板亲签，验证报告页展示
            page.request.post(
                f"{base_url}/api/admin/comment/{sid}?token={config.ADMIN_TOKEN}",
                data=json.dumps({"text": "老板亲签：演示通过，继续打磨。"}),
                headers={"Content-Type": "application/json"},
            )
            print("[支付] 已标记支付并放行")

        # 1) 初评（judging 阶段只有"已就位"，无分数）
        page.wait_for_function(
            """() => document.querySelectorAll('.judge.ready').length === 5
                     || document.querySelector('#dot').classList.contains('fail')""",
            timeout=420_000,
        )
        if failed():
            dump_failure()
            browser.close()
            return 1
        print("[初评] 5 位评委已就位")
        judging_scores = page.evaluate(
            "Array.from(document.querySelectorAll('.judge-final-score')).length"
        )
        if judging_scores != 0:
            print("❌ judging 阶段出现了分数")
            browser.close()
            return 1

        # 2) 自我介绍
        page.wait_for_function(
            """() => !document.querySelector('#intro-box').classList.contains('hidden')
                     || document.querySelector('#dot').classList.contains('fail')""",
            timeout=60_000,
        )
        if failed():
            dump_failure()
            browser.close()
            return 1
        page.fill(
            "#intro-text",
            "我们是窦柘小队，做了一个 AI 简历优化工具：上传简历、流式改写、导出 PDF。"
            "目前 3 个模板，日均 200 次调用，接下来重点做限流和评测集。",
        )
        page.click("#intro-submit")
        page.wait_for_function(
            "document.querySelector('#intro-box').classList.contains('hidden')",
            timeout=60_000,
        )
        print("[自我介绍] 已提交")

        # 3) 答辩
        answered = 0
        for _ in range(6):
            page.wait_for_function(
                """() => {
                    const box = document.querySelector('#answer-box');
                    const phase = document.querySelector('#phase').textContent;
                    return !box.classList.contains('hidden')
                        || ['评委合议', '亮椅', '生成报告', '评审完成'].includes(phase)
                        || document.querySelector('#dot').classList.contains('fail');
                }""",
                timeout=120_000,
            )
            if failed():
                dump_failure()
                browser.close()
                return 1
            if page.locator("#answer-box").is_hidden():
                break
            page.fill(
                "#answer-text",
                f"第 {answered + 1} 问：核心链路是 FastAPI + 流式输出，"
                "限流用令牌桶；评测集正在补，目前有 30 条人工样本。",
            )
            page.click("#answer-submit")
            page.wait_for_function(
                "document.querySelector('#answer-box').classList.contains('hidden')",
                timeout=90_000,
            )
            answered += 1
        print(f"[答辩] 已回答 {answered} 问")

        # 3.5) 舞台亮椅：评委图切到通过/不通过，选手情绪图切换
        page.wait_for_function(
            """() => document.querySelectorAll('.stage-judge[data-state="pass"], .stage-judge[data-state="fail"]').length >= 4""",
            timeout=180000,
        )
        stage_states = page.evaluate(
            """() => Array.from(document.querySelectorAll('.stage-judge')).map(n => n.dataset.id + ':' + n.dataset.state)"""
        )
        print("[舞台] 评委状态:", stage_states)
        page.screenshot(path=str(ROOT / "stage_reveal.png"), full_page=True)

        # 4) 报告页
        page.wait_for_function(
            """() => !document.querySelector('#screen-report').classList.contains('hidden')
                     || document.querySelector('#dot').classList.contains('fail')""",
            timeout=420_000,
        )
        if failed():
            dump_failure()
            browser.close()
            return 1
        page.wait_for_function(
            "document.querySelector('#report-summary').textContent.includes('终评')",
            timeout=300_000,
        )
        owner_visible = not page.locator("#owner-comment").is_hidden()
        screenshot = ROOT / "t7_screenshot.png"
        page.screenshot(path=str(screenshot), full_page=True)

        href = page.get_attribute("#report-link", "href")
        resp = page.request.get(base_url + href)
        report_ok = resp.ok and "预评审报告" in resp.text()

        print("[状态栏]", page.inner_text("#status"))
        print("[评委数]", page.locator(".judge").count())
        for i in range(5):
            card = page.locator(".judge").nth(i)
            print(
                f"  - {card.locator('.judge-name').inner_text()} | "
                f"{card.locator('.judge-verdict').inner_text()} | "
                f"{card.locator('.judge-final').inner_text() if card.locator('.judge-final').count() else '（无终评）'}"
            )
        page.wait_for_function(
            "document.querySelector('#phase').textContent.includes('评审完成')",
            timeout=60_000,
        )
        contestant_src = page.get_attribute("#contestant-img", "src")
        print("[老板亲签]", "显示" if owner_visible else "无")
        print("[报告]", href, "→", resp.status, len(resp.text()), "字")
        print("[选手终态]", contestant_src)
        print("[截图]", screenshot)

        # 5) 刷新恢复
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            """() => !document.querySelector('#screen-report').classList.contains('hidden')
                     && document.querySelector('#report-summary').textContent.includes('终评')""",
            timeout=60_000,
        )
        print("[刷新恢复] 通过")

        # 测试单归一：避免 E2E 数据污染营业流水
        if config.ADMIN_TOKEN and e2e_session_id:
            page.request.post(
                f"{base_url}/api/admin/test/{e2e_session_id}?token={config.ADMIN_TOKEN}",
                data=json.dumps({"is_test": True}),
                headers={"Content-Type": "application/json"},
            )

        browser.close()

    real_errors = [e for e in errors if "favicon" not in e.lower()]
    if real_errors:
        print("❌ 浏览器控制台报错：")
        for err in real_errors:
            print("   ", err)
        return 1

    if not report_ok:
        print("❌ 报告下载失败")
        return 1

    print("✅ D3 营业版浏览器全流程通过（预检 → 支付 → 评审 → 报告）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
