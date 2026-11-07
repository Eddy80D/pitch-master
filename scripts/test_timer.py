"""限时显示补丁浏览器自测。

前置服务需用短超时启动：
PAYMENT_REQUIRED=False INTRO_TIMEOUT_S=8 QA_ANSWER_TIMEOUT_S=12 QA_MAX_QUESTIONS=2

覆盖：介绍倒计时 → 最后 5 秒变红 → 自动推进；答辩倒计时 → 进度条缩短 →
超时文案；下一题提交后倒计时清除。
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
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8124"
    repo_url = sys.argv[2] if len(sys.argv) > 2 else "https://github.com/octocat/Hello-World"

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.goto(base_url, wait_until="domcontentloaded")
        page.fill("#repo", repo_url)
        page.click("#form-submit")

        page.wait_for_function(
            "document.querySelectorAll('.judge.ready').length === 5",
            timeout=300_000,
        )
        print("[初评] 5 位评委已就位")

        # ---- 自我介绍倒计时
        page.wait_for_selector("#intro-box:not(.hidden)", timeout=60_000)
        intro_text = page.inner_text("#intro-timer")
        assert "限时 8" in intro_text, intro_text
        assert "warn" not in (page.get_attribute("#intro-timer", "class") or "")
        print("[介绍] 初始文案：", intro_text)

        page.wait_for_function(
            "document.querySelector('#intro-timer').classList.contains('warn')",
            timeout=8_000,
        )
        print("[介绍] 最后 5 秒已变红")

        page.wait_for_function(
            "document.querySelector('#intro-box').classList.contains('hidden')",
            timeout=15_000,
        )
        assert "用户未作介绍" in page.inner_text("#log")
        print("[介绍] 超时自动推进，未作介绍")

        # ---- 第一题：进度条 + 超时
        page.wait_for_selector("#answer-box:not(.hidden)", timeout=30_000)
        q_text = page.inner_text("#q-timer")
        assert "限时 12" in q_text, q_text
        width0 = page.evaluate(
            "parseFloat(getComputedStyle(document.querySelector('#q-timer-bar')).width)"
        )
        page.wait_for_timeout(3000)
        width1 = page.evaluate(
            "parseFloat(getComputedStyle(document.querySelector('#q-timer-bar')).width)"
        )
        assert width1 < width0, (width0, width1)
        print(f"[答辩] 进度条 {width0:.0f}px → {width1:.0f}px，正在缩短")

        page.wait_for_function(
            "document.querySelector('#log').textContent.includes('时间到，视为跳过本题')",
            timeout=25_000,
        )
        print("[答辩] 超时文案已出现")

        # ---- 第二题：提交后倒计时清除
        page.wait_for_selector("#answer-box:not(.hidden)", timeout=30_000)
        page.fill("#answer-text", "核心链路是 FastAPI + 流式输出，限流用令牌桶。")
        page.click("#answer-submit")
        page.wait_for_function(
            "document.querySelector('#answer-box').classList.contains('hidden')",
            timeout=60_000,
        )
        page.wait_for_timeout(500)
        assert page.inner_text("#q-timer") == "", page.inner_text("#q-timer")
        assert page.locator("#q-timer-bar-wrap").is_hidden()
        print("[答辩] 提交后倒计时已清除")

        page.wait_for_function(
            "!document.querySelector('#screen-report').classList.contains('hidden')",
            timeout=300_000,
        )
        page.wait_for_function(
            "document.querySelector('#report-summary').textContent.includes('终评')",
            timeout=300_000,
        )
        page.screenshot(path=str(ROOT / "timer_screenshot.png"), full_page=True)
        print("[完成] 报告页已到达")
        print("✅ 限时显示补丁浏览器自测通过")
        browser.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
