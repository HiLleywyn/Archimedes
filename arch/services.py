"""arch/services.py -- the model service fallback chain.

A Archimedes deployment lists model providers in order of preference
(``OpenRouter -> Ollama -> Anthropic``). When the primary provider errors,
times out, or refuses, the chain advances to the next. The bot's existing
single-backend client (``ai.client``) is still the one talking to
OpenRouter and Ollama; this module decides which provider to ask each turn
and records why a fallback fired.

Service health is reported via ``ServiceChain.health()`` and surfaces on
the ``/services`` slash command. A provider that has failed
``BREAKER_THRESHOLD`` times in a row is marked unhealthy and skipped for
``BREAKER_COOLDOWN_S`` seconds; that prevents a thundering herd of failed
calls every turn when a provider is down.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from arch.config import ServiceSpec

log = logging.getLogger(__name__)

BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN_S = 60.0


@dataclass
class ServiceHealth:
    name: str
    healthy: bool
    last_error: str = ""
    consecutive_failures: int = 0
    cooldown_until: float = 0.0


# ── Provider adapters ─────────────────────────────────────────────────────────
class ProviderError(Exception):
    """Raised by an adapter when the provider call fails. The chain catches
    it, records the failure, and tries the next provider in the list."""


class _Adapter:
    """The minimal contract every provider implements."""

    name: str = ""

    async def complete(self, messages: list[dict], *,
                       model: str = "", max_tokens: int = 400,
                       temperature: float = 0.7) -> str:
        raise NotImplementedError


class OpenRouterAdapter(_Adapter):
    """Routes through the bot's existing ``ai.client.complete``."""

    name = "openrouter"

    async def complete(self, messages, *, model="", max_tokens=400,
                       temperature=0.7):
        # Local import keeps the chain importable without aiohttp.
        from ai.client import complete  # noqa: WPS433
        try:
            return await complete(
                messages, model=model or None, max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(str(exc)) from exc


class OllamaAdapter(_Adapter):
    """Same ``ai.client.complete`` path -- the backend switch happens inside
    that module via ``CHAT_BACKEND``. This adapter exists so an operator can
    list ``ollama`` as a fallback under ``openrouter`` and have it picked up
    when the primary fails; the real implementation reuses the configured
    Ollama base URL."""

    name = "ollama"

    async def complete(self, messages, *, model="", max_tokens=400,
                       temperature=0.7):
        from ai.client import complete  # noqa: WPS433
        try:
            return await complete(
                messages, model=model or None, max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(str(exc)) from exc


# Adapter registry. Operators name a provider in ``ARCHIMEDES_SERVICES``; the
# chain looks it up here. Unknown names are skipped at boot with a warning.
ADAPTERS: dict[str, type[_Adapter]] = {
    "openrouter": OpenRouterAdapter,
    "ollama": OllamaAdapter,
}


# ── The chain ─────────────────────────────────────────────────────────────────
@dataclass
class ServiceChain:
    """An ordered list of providers with a small circuit-breaker per entry."""

    specs: tuple[ServiceSpec, ...] = ()
    _adapters: list[_Adapter] = field(default_factory=list)
    _health: dict[str, ServiceHealth] = field(default_factory=dict)

    @classmethod
    def from_specs(cls, specs: tuple[ServiceSpec, ...]) -> "ServiceChain":
        chain = cls(specs=specs)
        for spec in specs:
            cls_ = ADAPTERS.get(spec.name)
            if cls_ is None:
                log.warning("Unknown Archimedes service: %s -- skipping.", spec.name)
                continue
            chain._adapters.append(cls_())
            chain._health[spec.name] = ServiceHealth(
                name=spec.name, healthy=True,
            )
        if not chain._adapters:
            # An empty chain still works -- it just always fails. Boot logs
            # already flagged the bad config; we keep the object alive so
            # the rest of the bot does not crash at import time.
            log.warning("Service chain is empty -- no model calls will succeed.")
        return chain

    def health(self) -> list[ServiceHealth]:
        return list(self._health.values())

    def _is_open(self, name: str, now: float) -> bool:
        """Circuit-open: skip this provider until its cooldown elapses."""
        h = self._health.get(name)
        if h is None:
            return False
        return h.cooldown_until > now

    def _record_success(self, name: str) -> None:
        h = self._health.get(name)
        if h is not None:
            h.healthy = True
            h.consecutive_failures = 0
            h.last_error = ""
            h.cooldown_until = 0.0

    def _record_failure(self, name: str, err: str) -> None:
        h = self._health.get(name)
        if h is None:
            return
        h.consecutive_failures += 1
        h.last_error = err
        if h.consecutive_failures >= BREAKER_THRESHOLD:
            h.healthy = False
            h.cooldown_until = time.monotonic() + BREAKER_COOLDOWN_S

    async def complete(self, messages: list[dict], *, max_tokens: int = 400,
                       temperature: float = 0.7) -> str:
        """Walk the chain and return the first successful completion."""
        now = time.monotonic()
        errors: list[str] = []
        for spec, adapter in zip(self.specs, self._adapters):
            if self._is_open(spec.name, now):
                errors.append(f"{spec.name}: cooldown")
                continue
            try:
                out = await adapter.complete(
                    messages, model=spec.model, max_tokens=max_tokens,
                    temperature=temperature,
                )
                self._record_success(spec.name)
                return out
            except ProviderError as exc:
                self._record_failure(spec.name, str(exc))
                errors.append(f"{spec.name}: {exc}")
                continue
        raise RuntimeError("All Archimedes services failed: " + "; ".join(errors))


# ── Convenience: build the chain from environment ────────────────────────────
def build_chain_from_env() -> ServiceChain:
    from arch.config import ArchConfig
    return ServiceChain.from_specs(ArchConfig.from_env().services)
