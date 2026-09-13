"""Cache layer: distributed lock and rate limiting."""

from __future__ import annotations

import pytest
from fakeredis import FakeAsyncRedis
from redis.exceptions import RedisError

from pycommon.cache import (
    InMemoryRateLimiter,
    LockAcquireError,
    RedisRateLimiter,
    RedisSlidingWindowRateLimiter,
    create_redis,
    redis_lock,
)
from pycommon.config import RedisSettings


@pytest.fixture
async def redis() -> FakeAsyncRedis:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


async def test_lock_acquire_and_release(redis: FakeAsyncRedis) -> None:
    async with redis_lock(redis, "job:1", ttl_seconds=5):
        assert await redis.exists("lock:job:1")
    assert not await redis.exists("lock:job:1")


async def test_lock_contention_raises(redis: FakeAsyncRedis) -> None:
    async with redis_lock(redis, "job:1", ttl_seconds=5):
        with pytest.raises(LockAcquireError):
            async with redis_lock(redis, "job:1", ttl_seconds=5, blocking_timeout=0.05):
                pass


async def test_lock_released_on_error(redis: FakeAsyncRedis) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with redis_lock(redis, "job:1", ttl_seconds=5):
            raise RuntimeError("boom")
    assert not await redis.exists("lock:job:1")


async def test_redis_rate_limiter_fixed_window(redis: FakeAsyncRedis) -> None:
    limiter = RedisRateLimiter(client=redis)

    for i in range(3):
        result = await limiter.hit("user-1", times=3, seconds=60)
        assert result.allowed, f"hit {i + 1} should be allowed"

    result = await limiter.hit("user-1", times=3, seconds=60)
    assert not result.allowed
    assert result.remaining == 0

    # Different key has its own window.
    other = await limiter.hit("user-2", times=3, seconds=60)
    assert other.allowed


async def test_in_memory_rate_limiter() -> None:
    limiter = InMemoryRateLimiter()
    assert (await limiter.hit("k", times=2, seconds=60)).allowed
    assert (await limiter.hit("k", times=2, seconds=60)).allowed
    assert not (await limiter.hit("k", times=2, seconds=60)).allowed


async def test_rate_limit_dependency() -> None:
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from pycommon.http.middleware.rate_limit import build_rate_limit_dep

    limiter = InMemoryRateLimiter()
    dep = build_rate_limit_dep(limiter, times=2, seconds=60)

    app = FastAPI()

    @app.get("/limited", dependencies=[Depends(dep)])
    async def limited() -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(app)
    assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 200
    resp = client.get("/limited")
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


async def test_auto_extend_loop_never_propagates() -> None:
    """A dead Redis must stop the extend loop quietly, not kill the caller.

    Anything escaping this task is re-raised when redis_lock reaps it, which
    skips lock.release() and masks the guarded block's own exception.
    """
    from unittest.mock import AsyncMock

    from pycommon.cache.lock import _auto_extend_loop

    lock = AsyncMock()
    lock.extend.side_effect = ConnectionError("redis down")

    await _auto_extend_loop(lock, ttl_seconds=0.02, key="job:1")

    lock.extend.assert_awaited()


async def test_lock_released_when_extend_task_dies(
    redis: FakeAsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock is released even if reaping the extend task raises.

    Regression: `await extend_task` re-raised the task's error inside the
    finally block, so lock.release() never ran and the key stayed held for the
    whole TTL while the original exception was replaced by a Redis one.
    """
    import pycommon.cache.lock as lock_module

    async def dying_loop(lock: object, ttl_seconds: float, key: str) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(lock_module, "_auto_extend_loop", dying_loop)

    async with redis_lock(redis, "job:1", ttl_seconds=5, auto_extend=True):
        assert await redis.exists("lock:job:1")

    assert not await redis.exists("lock:job:1")


async def test_original_error_survives_failing_extend_task(
    redis: FakeAsyncRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The block's own exception must reach the caller, not the infra one."""
    import pycommon.cache.lock as lock_module

    async def dying_loop(lock: object, ttl_seconds: float, key: str) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(lock_module, "_auto_extend_loop", dying_loop)

    with pytest.raises(ValueError, match="business rule"):
        async with redis_lock(redis, "job:1", ttl_seconds=5, auto_extend=True):
            raise ValueError("business rule")

    assert not await redis.exists("lock:job:1")


# --- rate DSL -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        ("100/minute", (100, 60.0)),
        ("5/s", (5, 1.0)),
        ("10/15seconds", (10, 15.0)),
        ("100 per 2 minutes", (100, 120.0)),
        ("1000/hour", (1000, 3600.0)),
        ("2/day", (2, 86400.0)),
        ((7, 3.5), (7, 3.5)),
    ],
)
def test_parse_rate(rate: object, expected: tuple[int, float]) -> None:
    from pycommon.cache import parse_rate

    assert parse_rate(rate) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("rate", ["", "abc", "100/", "100/fortnight", "0/minute", "-1/minute"])
def test_parse_rate_rejects_garbage(rate: str) -> None:
    from pycommon.cache import parse_rate

    with pytest.raises(ValueError):
        parse_rate(rate)


# --- fail-open ----------------------------------------------------------------


class _BrokenRedis:
    """Stands in for a Redis that has gone away mid-flight."""

    def register_script(self, script: str) -> object:
        async def _run(**kwargs: object) -> object:
            raise RedisError("connection lost")

        return _run


async def test_rate_limiter_fails_open_when_redis_is_down() -> None:
    """A rate limiter must not be able to take the API down with it."""
    limiter = RedisRateLimiter(client=_BrokenRedis())  # type: ignore[arg-type]

    result = await limiter.hit("user-1", times=1, seconds=60)

    assert result.allowed
    assert result.degraded, "degraded traffic must be distinguishable in metrics"


async def test_rate_limiter_can_fail_closed() -> None:
    """Where exceeding the limit is worse than rejecting traffic, opt out."""
    limiter = RedisRateLimiter(client=_BrokenRedis(), fail_open=False)  # type: ignore[arg-type]

    with pytest.raises(RedisError):
        await limiter.hit("user-1", times=1, seconds=60)


async def test_healthy_result_is_not_degraded(redis: FakeAsyncRedis) -> None:
    limiter = RedisRateLimiter(client=redis)
    result = await limiter.hit("user-1", times=3, seconds=60)
    assert not result.degraded
    assert result.limit == 3


# --- sliding window -----------------------------------------------------------


async def test_sliding_window_allows_up_to_limit(redis: FakeAsyncRedis) -> None:
    limiter = RedisSlidingWindowRateLimiter(client=redis)

    for i in range(3):
        assert (await limiter.hit("user-1", times=3, seconds=60)).allowed, f"hit {i + 1}"

    denied = await limiter.hit("user-1", times=3, seconds=60)
    assert not denied.allowed
    assert denied.remaining == 0
    assert denied.reset_after_seconds > 0

    assert (await limiter.hit("user-2", times=3, seconds=60)).allowed


async def test_sliding_window_does_not_record_denied_hits(redis: FakeAsyncRedis) -> None:
    """A client hammering a closed limit must not keep pushing the window forward."""
    limiter = RedisSlidingWindowRateLimiter(client=redis)

    await limiter.hit("user-1", times=1, seconds=60)
    for _ in range(5):
        assert not (await limiter.hit("user-1", times=1, seconds=60)).allowed

    assert await redis.zcard("rate_limit:user-1") == 1


# --- in-memory bound ----------------------------------------------------------


async def test_in_memory_limiter_is_bounded() -> None:
    """Unbounded, the window map grows one entry per client IP and never shrinks."""
    limiter = InMemoryRateLimiter(max_keys=50)

    for i in range(500):
        await limiter.hit(f"ip-{i}", times=10, seconds=60)

    assert len(limiter._windows) == 50
    # Eviction is oldest-first, so the most recent keys survive.
    assert "ip-499" in limiter._windows
    assert "ip-0" not in limiter._windows


async def test_rate_limit_dependency_emits_headers() -> None:
    """Clients should be able to pace themselves, not discover the limit by hitting it."""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from pycommon.http.middleware.rate_limit import build_rate_limit_dep

    dep = build_rate_limit_dep(InMemoryRateLimiter(), "2/minute")
    app = FastAPI()

    @app.get("/limited", dependencies=[Depends(dep)])
    async def limited() -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(app)

    first = client.get("/limited")
    assert first.status_code == 200
    assert first.headers["X-RateLimit-Limit"] == "2"
    assert first.headers["X-RateLimit-Remaining"] == "1"
    assert int(first.headers["X-RateLimit-Reset"]) > 0

    client.get("/limited")
    blocked = client.get("/limited")
    assert blocked.status_code == 429
    assert blocked.headers["X-RateLimit-Remaining"] == "0"
    assert blocked.headers["Retry-After"]


def test_rate_limit_dep_requires_a_rate() -> None:
    from pycommon.http.middleware.rate_limit import build_rate_limit_dep

    with pytest.raises(ValueError, match="Pass a rate"):
        build_rate_limit_dep(InMemoryRateLimiter())


# --- client factory -------------------------------------------------------


def test_create_redis_applies_every_setting() -> None:
    """The timeouts are the point of this factory.

    redis-py blocks forever without ``socket_timeout`` and
    ``socket_connect_timeout``, so a hung connection pins an event-loop task
    instead of failing fast. Losing one of these would look like nothing at all
    until a Redis node stopped answering mid-connection.
    """
    settings = RedisSettings(
        url="redis://localhost:6379/3",
        max_connections=7,
        socket_timeout_seconds=1.5,
        socket_connect_timeout_seconds=2.5,
        health_check_interval_seconds=11,
        retry_on_timeout=False,
    )

    client = create_redis(settings)
    pool = client.connection_pool
    kwargs = pool.connection_kwargs

    assert pool.max_connections == 7
    assert kwargs["socket_timeout"] == 1.5
    assert kwargs["socket_connect_timeout"] == 2.5
    assert kwargs["health_check_interval"] == 11
    assert kwargs["retry_on_timeout"] is False
    # Keepalive is not a setting: a pool behind a load balancer needs it either
    # way, so it is applied unconditionally.
    assert kwargs["socket_keepalive"] is True
    # The database from the URL has to survive, or a service quietly reads and
    # writes someone else's keyspace.
    assert kwargs["db"] == 3
    assert kwargs["decode_responses"] is True


def test_create_redis_can_return_bytes() -> None:
    """Anything storing pickled or compressed values needs the raw bytes."""
    client = create_redis(RedisSettings(), decode_responses=False)

    assert client.connection_pool.connection_kwargs["decode_responses"] is False


def test_each_call_builds_its_own_pool() -> None:
    """Documented behaviour, and the reason to build the client once per process.

    Calling this per request would give every request its own pool and defeat
    pooling entirely, which is a slow leak rather than an error.
    """
    settings = RedisSettings()

    first = create_redis(settings)
    second = create_redis(settings)

    assert first.connection_pool is not second.connection_pool
