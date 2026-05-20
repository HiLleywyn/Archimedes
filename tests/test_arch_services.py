"""tests/test_arch_services.py -- ServiceChain fallback and circuit breaker."""
from __future__ import annotations

import pytest

from arch.config import ServiceSpec
from arch.services import (
    BREAKER_THRESHOLD, ProviderError, ServiceChain, ServiceHealth,
    _Adapter,
)


class _StubAdapter(_Adapter):
    """Configurable test adapter -- never touches the network."""

    def __init__(self, name: str, *, fail_times: int = 0,
                 reply: str = "ok", exc: Exception | None = None) -> None:
        self.name = name
        self.calls = 0
        self.fail_times = fail_times
        self.reply = reply
        self.exc = exc

    async def complete(self, messages, *, model="", max_tokens=400,
                       temperature=0.7):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        if self.calls <= self.fail_times:
            raise ProviderError(f"{self.name} simulated failure {self.calls}")
        return self.reply


def _chain_with(*adapters: _Adapter) -> ServiceChain:
    chain = ServiceChain(
        specs=tuple(ServiceSpec(name=a.name, model="") for a in adapters),
    )
    chain._adapters = list(adapters)
    chain._health = {
        a.name: ServiceHealth(name=a.name, healthy=True) for a in adapters
    }
    return chain


async def test_first_healthy_provider_returns_immediately() -> None:
    primary = _StubAdapter("primary", reply="primary-said-hello")
    backup = _StubAdapter("backup", reply="backup-said-hello")
    chain = _chain_with(primary, backup)
    out = await chain.complete([{"role": "user", "content": "hi"}])
    assert out == "primary-said-hello"
    assert primary.calls == 1
    assert backup.calls == 0


async def test_falls_back_when_primary_errors() -> None:
    primary = _StubAdapter("primary", fail_times=1)
    backup = _StubAdapter("backup", reply="backup-took-over")
    chain = _chain_with(primary, backup)
    out = await chain.complete([{"role": "user", "content": "hi"}])
    assert out == "backup-took-over"
    assert primary.calls == 1
    assert backup.calls == 1


async def test_raises_when_every_provider_fails() -> None:
    a = _StubAdapter("a", fail_times=10)
    b = _StubAdapter("b", fail_times=10)
    chain = _chain_with(a, b)
    with pytest.raises(RuntimeError) as exc_info:
        await chain.complete([{"role": "user", "content": "hi"}])
    assert "a:" in str(exc_info.value)
    assert "b:" in str(exc_info.value)


async def test_circuit_breaker_opens_after_threshold_failures() -> None:
    a = _StubAdapter("a", fail_times=999)
    b = _StubAdapter("b", reply="ok")
    chain = _chain_with(a, b)
    for _ in range(BREAKER_THRESHOLD):
        await chain.complete([{"role": "user", "content": "hi"}])
    # Now a's circuit should be open -- a fresh call must not invoke it.
    a.calls = 0
    out = await chain.complete([{"role": "user", "content": "hi"}])
    assert out == "ok"
    assert a.calls == 0
    assert chain._health["a"].healthy is False


async def test_health_resets_after_a_success() -> None:
    a = _StubAdapter("a", fail_times=1)
    chain = _chain_with(a)
    # First call fails -- the chain raises because there is no backup.
    with pytest.raises(RuntimeError):
        await chain.complete([{"role": "user", "content": "hi"}])
    assert chain._health["a"].consecutive_failures == 1
    # Second call succeeds (fail_times was 1) -- failure counter resets.
    await chain.complete([{"role": "user", "content": "hi"}])
    assert chain._health["a"].consecutive_failures == 0
    assert chain._health["a"].healthy is True
