"""T16 浏览器端到端：初评 → 自我介绍 → 答辩 → 复议/互怼/亮椅 → 报告。

用法：python scripts/test_e2e.py [base_url] [repo_url]
前置：服务已启动；本机有 Chrome（playwright 使用 channel=chrome）。
注意：会调用真实 MiMo API（约 20 次），请控制运行次数。
"""

from __future__ import annotations

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

        page.goto(base_url, wait_until="domcontentloaded")
        page.fill("#repo", repo_url)
        page.click("#form-submit")

        def failed() -> bool:
            return page.locator("#dot.fail").count() > 0

        def dump_failure() -> None:
            print("[失败] 流水线报错，日志：")
            print(page.inner_text("#log"))
            page.screenshot(path=str(ROOT / "e2e_failure.png"), full_page=True)

        # 1) 初评（同时监听错误，避免网络失败时干等 5 分钟）
        page.wait_for_function(
            """() => document.querySelectorAll('.judge').length === 5
                     || document.querySelector('#dot').classList.contains('fail')""",
            timeout=420_000,
        )
        if failed():
            dump_failure()
            browser.close()
            return 1
        print("[初评] 5 位评委已出现")

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

        # 3) 答辩（演示模式默认 4 问；每问提交后等它关闭）
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

        # 4) 报告
        page.wait_for_function(
            """() => !document.querySelector('#report-box').classList.contains('hidden')
                     || document.querySelector('#dot').classList.contains('fail')""",
            timeout=420_000,
        )
        if failed():
            dump_failure()
            browser.close()
            return 1
        page.wait_for_function(
            "document.querySelector('#phase').textContent.includes('评审完成')",
            timeout=60_000,
        )
        screenshot = ROOT / "t7_screenshot.png"
        page.screenshot(path=str(screenshot), full_page=True)

        href = page.get_attribute("#report-link", "href")
        resp = page.request.get(base_url + href)
        report_ok = resp.ok and "预评审报告" in resp.text()

        print("[状态栏]", page.inner_text("#status"))
        print("[评委数]", page.locator(".judge").count())
        for i in range(5):
            card = page.locator(".judge").nth(i)
            final = card.locator(".judge-final")
            print(
                f"  - {card.locator('.judge-name').inner_text()} | "
                f"{card.locator('.judge-score').inner_text()} | "
                f"{final.inner_text() if final.count() else '（无终评）'}"
            )
        print("[报告]", href, "→", resp.status, len(resp.text()), "字")
        print("[截图]", screenshot)

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

    print("✅ T16 浏览器全流程通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
