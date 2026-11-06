"""T7 浏览器端到端自测：打开页面 → 提交仓库 → 等 5 位评委卡片出现。

用法：python scripts/test_e2e.py [base_url] [repo_url]
前置：服务已启动；本机有 Chrome（playwright 使用 channel=chrome）。
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
        page.on("console", lambda msg: errors.append(f"console: {msg.text}") if msg.type == "error" else None)

        page.goto(base_url, wait_until="domcontentloaded")
        page.fill("#repo", repo_url)
        page.click("#start")

        page.wait_for_function(
            "document.querySelectorAll('.judge').length === 5",
            timeout=300_000,
        )
        page.wait_for_function(
            "document.querySelector('#phase').textContent.includes('初评完成')",
            timeout=60_000,
        )

        screenshot = ROOT / "t7_screenshot.png"
        page.screenshot(path=str(screenshot), full_page=True)

        print("[状态栏]", page.inner_text("#status"))
        print("[评委数]", page.locator(".judge").count())
        for i in range(5):
            card = page.locator(".judge").nth(i)
            print(f"  - {card.locator('.judge-name').inner_text()} | {card.locator('.judge-score').inner_text()} | {card.locator('.judge-verdict').inner_text()}")
        print("[日志尾部]", page.inner_text("#log").splitlines()[-3:])
        print("[截图]", screenshot)

        browser.close()

    real_errors = [e for e in errors if "favicon" not in e.lower()]
    if real_errors:
        print("❌ 浏览器控制台报错：")
        for err in real_errors:
            print("   ", err)
        return 1

    print("✅ T7 浏览器端到端通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
