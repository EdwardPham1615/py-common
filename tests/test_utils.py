"""Utils: ids, clock, retry (tenacity), circuit breaker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from py_common.testing.fakes import FakeUnitOfWork, InMemoryRepository
from py_common.utils import (
    ALPHABET_NUMERIC,
    ALPHABET_UPPER_NUMERIC,
    AsyncCircuitBreaker,
    CircuitOpenError,
    CircuitState,
    FixedClock,
    SystemClock,
    new_nanoid,
    new_uuid7,
    retry_async,
    utcnow,
)


def test_nanoid_default_length() -> None:
    assert len(new_nanoid()) == 21


def test_nanoid_custom_alphabet() -> None:
    nid = new_nanoid(8, alphabet=ALPHABET_UPPER_NUMERIC)
    assert len(nid) == 8
    assert all(c in ALPHABET_UPPER_NUMERIC for c in nid)


def test_nanoid_numeric_only() -> None:
    nid = new_nanoid(6, alphabet=ALPHABET_NUMERIC)
    assert nid.isdigit()


def test_nanoid_rejects_bad_args() -> None:
    with pytest.raises(ValueError):
        new_nanoid(0)
    with pytest.raises(ValueError):
        new_nanoid(alphabet="")


def test_nanoid_unique() -> None:
    ids = {new_nanoid() for _ in range(100)}
    assert len(ids) == 100


def test_uuid7_is_version_7() -> None:
    u = new_uuid7()
    assert u.version == 7


def test_uuid7_time_ordered() -> None:
    """Same-ms IDs stay monotonic via the counter; later ms sorts after earlier."""
    batch = [new_uuid7() for _ in range(50)]
    assert all(u.version == 7 for u in batch)
    assert batch == sorted(batch, key=lambda u: u.bytes)


def test_system_clock_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert utcnow().tzinfo is not None


def test_fixed_clock_freeze_and_advance() -> None:
    start = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    clock = FixedClock(start)
    assert clock.now() == start
    clock.advance(seconds=30)
    assert clock.now() == start + timedelta(seconds=30)
    clock.set(start)
    assert clock.now() == start


async def test_retry_succeeds_after_failures() -> None:
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("transient")
        return "ok"

    result = await retry_async(flaky, max_attempts=5, initial_backoff_seconds=0.001)
    assert result == "ok"
    assert attempts == 3


async def test_retry_reraises_after_max_attempts() -> None:
    attempts = 0

    async def always_fails() -> None:
        nonlocal attempts
        attempts += 1
        raise ConnectionError("down")

    with pytest.raises(ConnectionError, match="down"):
        await retry_async(always_fails, max_attempts=3, initial_backoff_seconds=0.001)
    assert attempts == 3


async def test_retry_non_retryable_propagates_immediately() -> None:
    attempts = 0

    async def wrong_error() -> None:
        nonlocal attempts
        attempts += 1
        raise ValueError("bug, not transient")

    with pytest.raises(ValueError, match="bug"):
        await retry_async(
            wrong_error,
            max_attempts=5,
            initial_backoff_seconds=0.001,
            retry_on=(ConnectionError,),
        )
    assert attempts == 1


async def test_circuit_breaker_opens_after_threshold() -> None:
    breaker = AsyncCircuitBreaker(failure_threshold=3, recovery_timeout=60, name="test")

    async def fail() -> None:
        raise ConnectionError("down")

    for _ in range(3):
        with pytest.raises(ConnectionError):
            await breaker.call(fail)

    assert breaker.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        await breaker.call(fail)


async def test_circuit_breaker_half_open_recovery() -> None:
    breaker = AsyncCircuitBreaker(failure_threshold=1, recovery_timeout=0.01, name="recover")

    async def fail() -> None:
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        await breaker.call(fail)
    assert breaker.state is CircuitState.OPEN

    import anyio

    await anyio.sleep(0.02)
    assert breaker.state is CircuitState.HALF_OPEN

    async def ok() -> str:
        return "ok"

    assert await breaker.call(ok) == "ok"
    assert breaker.state is CircuitState.CLOSED


async def test_circuit_breaker_context_manager() -> None:
    breaker = AsyncCircuitBreaker(failure_threshold=1, recovery_timeout=60)

    with pytest.raises(RuntimeError):
        async with breaker:
            raise RuntimeError("boom")
    assert breaker.state is CircuitState.OPEN


class _Entity:
    def __init__(self, id: int, name: str) -> None:
        self.id = id
        self.name = name


async def test_in_memory_repository_crud() -> None:
    repo: InMemoryRepository[_Entity, int] = InMemoryRepository()
    await repo.create(_Entity(1, "a"))
    await repo.create(_Entity(2, "b"))

    fetched = await repo.get(1)
    assert fetched is not None and fetched.name == "a"
    assert len(await repo.get_list()) == 2
    assert await repo.delete(1) is True
    assert await repo.delete(1) is False


async def test_fake_uow_commit_and_rollback() -> None:
    uow = FakeUnitOfWork()
    async with uow:
        pass
    assert uow.committed and not uow.rolled_back

    uow2 = FakeUnitOfWork()
    with pytest.raises(RuntimeError):
        async with uow2:
            raise RuntimeError("x")
    assert uow2.rolled_back and not uow2.committed
