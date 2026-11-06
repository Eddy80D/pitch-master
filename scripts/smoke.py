"""冒烟脚本：直接调一次 pro（非流式）+ 一次 flash（流式），并检查 usage 记账。

运行：python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，统一切到 UTF-8，避免中文/emoji 报错
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import config, mimo_client  # noqa: E402


def usage_lines() -> int:
    if not config.USAGE_FILE.exists():
        return 0
    return sum(1 for _ in config.USAGE_FILE.open("r", encoding="utf-8"))


async def main() -> int:
    config.ensure_dirs()
    config.validate()

    before = usage_lines()
    print(f"[配置] base_url={config.MIMO_BASE_URL}")
    print(f"[配置] pro={config.MODEL_PRO} flash={config.MODEL_FLASH}")

    print("\n[PRO 非流式] 等待中…")
    reply = await mimo_client.chat(
        "smoke-pro",
        config.MODEL_PRO,
        [{"role": "user", "content": "用一句话说明你能帮黑客松选手做什么。"}],
        timeout=120,
        max_tokens=300,
    )
    print("[PRO 回复]", reply or "(空)")

    print("\n[FLASH 流式] ", end="", flush=True)
    chunks = 0
    async for text in mimo_client.chat_stream(
        "smoke-flash",
        config.MODEL_FLASH,
        [{"role": "user", "content": "用一句话介绍你自己。"}],
        timeout=120,
        max_tokens=200,
    ):
        print(text, end="", flush=True)
        chunks += 1
    print(f"\n[FLASH] 收到 {chunks} 个文本块")

    after = usage_lines()
    added = after - before
    print(f"\n[记账] logs/usage.jsonl 新增 {added} 条")
    if added < 2:
        print("❌ 记账条数不足 2，请检查 mimo_client")
        return 1

    print("✅ T1 冒烟通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
