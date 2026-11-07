"""MiMo（OpenAI 兼容）客户端：chat / chat_stream / 重试 / token 记账。

约定：
- 网络错误或 5xx 才重试 1 次；4xx（鉴权/参数错）直接抛出。
- 每次真实调用结束都往 logs/usage.jsonl 追加一条记录（评审材料要用）。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextvars import ContextVar
from typing import Any, AsyncIterator

import httpx

from . import config

_write_lock = threading.Lock()

# 当前 Pipeline 所属 session；contextvar 会随 asyncio 任务继承
_session_var: ContextVar[str | None] = ContextVar("pitchmaster_session", default=None)


def set_session_id(session_id: str | None) -> None:
    _session_var.set(session_id)


def current_session_id() -> str | None:
    return _session_var.get()


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.MIMO_API_KEY}",
        "Content-Type": "application/json",
    }


def _url() -> str:
    return f"{config.MIMO_BASE_URL}/chat/completions"


def _record_usage(caller: str, model: str, usage: dict[str, Any] | None) -> None:
    """追加一条记账记录。任何异常都不允许影响主流程。"""
    usage = usage or {}
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ts_epoch": time.time(),
        "session_id": _session_var.get(),
        "model": model,
        "caller": caller,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }
    try:
        config.ensure_dirs()
        line = json.dumps(record, ensure_ascii=False)
        with _write_lock:
            with open(config.USAGE_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        pass


def _content_of(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return message.get("content") or ""


async def chat(
    caller: str,
    model: str,
    messages: list[dict[str, Any]],
    timeout: float | None = None,
    **kwargs: Any,
) -> str:
    """非流式调用，返回助手文本。"""
    payload: dict[str, Any] = {"model": model, "messages": messages, **kwargs}
    last_error: Exception | None = None

    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=timeout or config.MIMO_TIMEOUT) as client:
                # httpx 的 timeout 是单次读写超时；这里再加一道"整通调用"硬上限，
                # 防止模型慢慢吐字把整场评审拖住（实测出现过单次 7 分钟）。
                resp = await asyncio.wait_for(
                    client.post(_url(), headers=_headers(), json=payload),
                    timeout=timeout or config.MIMO_TIMEOUT,
                )
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"server error {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                data = resp.json()
                _record_usage(caller, model, data.get("usage"))
                return _content_of(data)
        except httpx.HTTPStatusError as exc:
            last_error = exc
            retryable = exc.response is not None and exc.response.status_code >= 500
            if attempt == 0 and retryable:
                await asyncio.sleep(1.0)
                continue
            raise
        except httpx.TransportError as exc:
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(1.0)
                continue
            raise

    raise last_error or RuntimeError("MiMo 调用失败")


async def chat_stream(
    caller: str,
    model: str,
    messages: list[dict[str, Any]],
    timeout: float | None = None,
    **kwargs: Any,
) -> AsyncIterator[str]:
    """流式调用，逐块 yield 正文文本（忽略 reasoning_content）。"""
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        **kwargs,
    }

    for attempt in range(2):
        emitted = False
        try:
            async for text in _stream_once(caller, model, payload, timeout):
                emitted = True
                yield text
            return
        except httpx.HTTPStatusError as exc:
            retryable = exc.response is not None and exc.response.status_code >= 500
            if attempt == 0 and retryable and not emitted:
                await asyncio.sleep(1.0)
                continue
            raise
        except httpx.TransportError:
            if attempt == 0 and not emitted:
                await asyncio.sleep(1.0)
                continue
            raise


async def _stream_once(
    caller: str,
    model: str,
    payload: dict[str, Any],
    timeout: float | None,
) -> AsyncIterator[str]:
    request_timeout = httpx.Timeout(timeout or config.MIMO_TIMEOUT, connect=30.0)
    async with httpx.AsyncClient(timeout=request_timeout) as client:
        async with client.stream("POST", _url(), headers=_headers(), json=payload) as resp:
            if resp.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"server error {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()

            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:") :].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                usage = chunk.get("usage")
                if usage:
                    _record_usage(caller, model, usage)

                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield text
