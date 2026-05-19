"""ai/client.py -- model provider client.

Talks to an OpenAI-compatible chat-completions endpoint. OpenRouter is the
default backend; a local Ollama instance is supported through its
``/v1`` OpenAI-compatible API so there is one code path for both.

Exposed surface:
  * ``complete``          -- one-shot non-streaming completion -> str
  * ``complete_default``  -- ``complete`` with the default chat model
  * ``stream_completion`` -- async generator of streaming events
  * ``generate_image``    -- one image from an OpenRouter image model
  * ``submit_video`` / ``poll_video`` -- the async OpenRouter video API
  * ``download_media``    -- fetch generated media bytes with the API key
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
                    log.warning("completion stream http %s: %s",
                                resp.status, body[:300])
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


# ── OpenRouter image and video generation ────────────────────────────────────
# Image and video generation always go through OpenRouter, whatever
# CHAT_BACKEND is. Image generation is one synchronous chat-completions call
# with an image modality; video generation is OpenRouter's asynchronous
# ``/videos`` API -- submit, then poll.
def _openrouter_base() -> str:
    return Config.OPENROUTER_BASE_URL.rstrip("/")


def _openrouter_headers() -> dict:
    """Headers for a direct OpenRouter call (not the configurable backend)."""
    headers = {
        "Content-Type": "application/json",
        "HTTP-Referer": Config.OPENROUTER_REFERER,
        "X-Title": Config.OPENROUTER_TITLE,
    }
    if Config.OPENROUTER_API_KEY:
        headers["Authorization"] = f"Bearer {Config.OPENROUTER_API_KEY}"
    return headers


def _api_error(data, status: int) -> str:
    """Pull a human-readable message out of an API error response."""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or f"http {status}")
        if err:
            return str(err)
        if data.get("message"):
            return str(data["message"])
    return f"http {status}"


async def generate_image(prompt: str, *, model: str, timeout: float = 120.0) -> dict:
    """Generate one image from an OpenRouter image-output model.

    Returns ``{"image": <data-or-http url>}`` on success, or
    ``{"error": "..."}`` on failure.
    """
    if not Config.OPENROUTER_API_KEY:
        return {"error": "OPENROUTER_API_KEY is not configured"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        # Image-only: a generation model has no text output, and asking for
        # text alongside it leaves OpenRouter with no matching endpoint (a
        # "no endpoints found" 404).
        "modalities": ["image"],
    }
    async with _sem():
        session = await _get_session()
        try:
            async with session.post(
                f"{_openrouter_base()}/chat/completions",
                headers=_openrouter_headers(), json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    message = _api_error(data, resp.status)
                    log.warning("image generation http %s: %s",
                                resp.status, message)
                    return {"error": message}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return {"error": f"image request failed: {exc}"}
        except ValueError:
            return {"error": "the image API returned an unreadable response"}
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return {"error": "the image model returned no message"}
    for image in message.get("images") or []:
        if not isinstance(image, dict):
            continue
        url = (image.get("image_url") or {}).get("url")
        if url:
            return {"image": url}
    return {"error": "the image model returned no image"}


async def submit_video(prompt: str, *, model: str, timeout: float = 60.0) -> dict:
    """Submit a video generation job to OpenRouter's async ``/videos`` API.

    Returns ``{"id": ..., "polling_url": ...}`` on success, ``{"error": ...}``
    on failure.
    """
    if not Config.OPENROUTER_API_KEY:
        return {"error": "OPENROUTER_API_KEY is not configured"}
    async with _sem():
        session = await _get_session()
        try:
            async with session.post(
                f"{_openrouter_base()}/videos",
                headers=_openrouter_headers(),
                json={"model": model, "prompt": prompt},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status not in (200, 201, 202):
                    log.warning("video submit http %s", resp.status)
                    return {"error": _api_error(data, resp.status)}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return {"error": f"video request failed: {exc}"}
        except ValueError:
            return {"error": "the video API returned an unreadable response"}
    job_id = data.get("id") if isinstance(data, dict) else None
    polling_url = data.get("polling_url") if isinstance(data, dict) else None
    if not job_id or not polling_url:
        return {"error": "the video API returned no job"}
    return {"id": str(job_id), "polling_url": str(polling_url)}


async def poll_video(polling_url: str, *, timeout: float = 30.0) -> dict:
    """Poll one video job once. Returns the raw status dict, or ``{"error": ...}``."""
    async with _sem():
        session = await _get_session()
        try:
            async with session.get(
                polling_url, headers=_openrouter_headers(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    return {"error": _api_error(data, resp.status)}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return {"error": f"poll failed: {exc}"}
        except ValueError:
            return {"error": "the video API returned an unreadable response"}
    return data if isinstance(data, dict) else {"error": "bad poll response"}


async def download_media(
    url: str, *, max_bytes: int = 25 * 1024 * 1024, timeout: float = 180.0,
) -> dict:
    """Download generated-media bytes, authenticating with the OpenRouter key.

    Returns ``{"data": bytes}`` on success, or ``{"error": "..."}``.
    """
    headers = {}
    if Config.OPENROUTER_API_KEY:
        headers["Authorization"] = f"Bearer {Config.OPENROUTER_API_KEY}"
    async with _sem():
        session = await _get_session()
        try:
            async with session.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    return {"error": f"download http {resp.status}"}
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > max_bytes:
                        return {"error": f"media exceeds the "
                                         f"{max_bytes // (1024 * 1024)}MB limit"}
                    chunks.append(chunk)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return {"error": f"download failed: {exc}"}
    return {"data": b"".join(chunks)}
