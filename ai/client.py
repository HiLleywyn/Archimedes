"""ai/client.py -- model provider client.

Talks to an OpenAI-compatible chat-completions endpoint. OpenRouter is the
default backend; a local Ollama instance is supported through its
``/v1`` OpenAI-compatible API so there is one code path for both.

Exposed surface:
  * ``complete``          -- one-shot non-streaming completion -> str
  * ``complete_default``  -- ``complete`` with the default chat model
  * ``stream_completion`` -- async generator of streaming events
  * ``close_client``      -- shut the shared aiohttp session down

A concurrency semaphore caps in-flight requests so a burst of chat traffic
cannot exhaust the provider's rate limit or the event loop.
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

from config import Config

log = logging.getLogger(__name__)

_session: aiohttp.ClientSession | None = None
_semaphore: asyncio.Semaphore | None = None


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, Config.AI_QUEUE_CAP))
    return _semaphore


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_client() -> None:
    """Close the shared HTTP session (called on shutdown)."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def _backend() -> tuple[str, str, str]:
    """Return (base_url, api_key, default_model) for the configured backend."""
    if Config.CHAT_BACKEND == "ollama":
        base = Config.OLLAMA_BASE_URL.rstrip("/") + "/v1"
        return base, "ollama", Config.OLLAMA_MODEL
    return Config.OPENROUTER_BASE_URL.rstrip("/"), Config.OPENROUTER_API_KEY, Config.OPENROUTER_MODEL


def _headers(api_key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if Config.CHAT_BACKEND == "openrouter":
        headers["HTTP-Referer"] = Config.OPENROUTER_REFERER
        headers["X-Title"] = Config.OPENROUTER_TITLE
    return headers


async def complete(
    messages: list[dict],
    *,
    model: str | None = None,
    max_tokens: int = 400,
    temperature: float = 0.85,
    tools: list[dict] | None = None,
    timeout: float = 60.0,
) -> str | None:
    """Run a single non-streaming completion. Returns the assistant text."""
    base, api_key, default_model = _backend()
    payload: dict = {
        "model": model or default_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools

    async with _sem():
        session = await _get_session()
        try:
            async with session.post(
                f"{base}/chat/completions",
                headers=_headers(api_key),
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning("completion http %s: %s", resp.status, body[:300])
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("completion request failed: %s", exc)
            return None

    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return None


async def complete_default(
    messages: list[dict], *, max_tokens: int = 400, temperature: float = 0.85,
    model: str | None = None,
) -> str | None:
    """``complete`` using the default chat model unless ``model`` is given."""
    return await complete(
        messages, model=model, max_tokens=max_tokens, temperature=temperature,
    )


async def stream_completion(
    messages: list[dict],
    *,
    model: str | None = None,
    max_tokens: int = 400,
    temperature: float = 0.85,
    tools: list[dict] | None = None,
    timeout: float = 90.0,
):
    """Stream a completion, yielding event dicts.

    Event types:
      ``{"type": "delta", "text": str}``           -- a chunk of assistant text
      ``{"type": "done", "text": str,
          "finish_reason": str, "tool_calls": list,
          "usage": dict}``                          -- the turn finished
      ``{"type": "error", "error": str}``           -- the request failed
    """
    base, api_key, default_model = _backend()
    payload: dict = {
        "model": model or default_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools

    text_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    finish_reason = ""
    usage: dict = {}

    async with _sem():
        session = await _get_session()
        try:
            async with session.post(
                f"{base}/chat/completions",
                headers=_headers(api_key),
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    yield {"type": "error", "error": f"http_{resp.status}: {body[:200]}"}
                    return
                async for raw in resp.content:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        obj = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("usage"):
                        usage = obj["usage"]
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        text_parts.append(piece)
                        yield {"type": "delta", "text": piece}
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tool_calls.setdefault(
                            idx, {"id": "", "type": "function",
                                   "function": {"name": "", "arguments": ""}},
                        )
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            yield {"type": "error", "error": f"network_{type(exc).__name__}"}
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("stream crashed: %s", exc)
            yield {"type": "error", "error": "stream_crash"}
            return

    yield {
        "type": "done",
        "text": "".join(text_parts),
        "finish_reason": finish_reason,
        "tool_calls": [tool_calls[i] for i in sorted(tool_calls)],
        "usage": usage,
    }
