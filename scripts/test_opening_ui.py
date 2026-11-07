"""开场 UI 自测：评委天团 6 卡、资料卡弹窗、零外链、移动端不溢出。

用法：python scripts/test_opening_ui.py [base_url]
前置：服务已启动；本机有 Chrome。
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

from app.agents.judges import load_judges  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

CARD_IDS = ["tech", "biz", "vision", "originality", "aesthetic", "player"]
EXTERNAL_MARKERS = ["fonts.googleapis", "fonts.gstatic", "cdn.", "unpkg", "jsdelivr"]


def static_checks() -> None:
    for card_id in CARD_IDS:
        path = ROOT / "static" / "cards" / f"{card_id}.html"
        assert path.exists(), path
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in EXTERNAL_MARKERS:
            assert marker not in text, f"{path} 含外链 {marker}"

    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    for marker in EXTERNAL_MARKERS:
        assert marker not in index, f"index.html 含外链 {marker}"

    names = [judge.get("display_name") or judge["name"] for judge in load_judges()]
    assert names == ["比特", "商业大亨", "远见博士", "摩斯", "洛克"], names
    print("✅ 静态检查：6 张资料卡存在、零外链、评委展示名正确")


def browser_checks(base_url: str) -> None:
    outside: list[str] = []
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 1200})
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on(
            "request",
            lambda req: outside.append(req.url)
            if not req.url.startswith(base_url)
            and not req.url.startswith(("data:", "about:"))
            else None,
        )
        page.goto(base_url, wait_until="domcontentloaded")
        page.wait_for_selector("#judge-lineup .judge-lineup-card", timeout=15000)

        names = page.locator("#judge-lineup .card-top").all_inner_texts()
        assert names == ["比特", "商业大亨", "远见博士", "摩斯", "洛克", "选手"], names
        assert page.evaluate(
            "getComputedStyle(document.querySelector('.hero-title')).fontSize"
        ) == "72px"

        for index, card_id in enumerate(CARD_IDS):
            page.locator("#judge-lineup .judge-lineup-card").nth(index).click()
            page.wait_for_function(
                "!document.querySelector('#card-modal').classList.contains('hidden')"
            )
            src = page.get_attribute("#card-frame", "src")
            assert src.endswith(f"/static/cards/{card_id}.html"), src
            page.wait_for_timeout(300)
            page.keyboard.press("Escape")
            page.wait_for_function(
                "document.querySelector('#card-modal').classList.contains('hidden')"
            )

        page.screenshot(path=str(ROOT / "opening_ui.png"), full_page=True)
        page.set_viewport_size({"width": 375, "height": 900})
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - window.innerWidth"
        )
        assert overflow <= 1, overflow

        browser.close()

    assert not outside, outside
    assert not errors, errors
    print("✅ 浏览器检查：6 卡可开可关、零外链请求、375px 无横向溢出")


def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8125"
    static_checks()
    browser_checks(base_url)
    print("✅ 开场 UI 自测全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
