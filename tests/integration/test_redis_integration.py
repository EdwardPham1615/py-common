"""What fakeredis cannot prove: real Lua, real server clock, real expiry."""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest
from redis.asyncio import Redis

from pycommon.cache import (
    Cache,
    LockAcquireError,
    RedisRateLimiter,
    RedisSlidingWindowRateLimiter,
    create_redis,
    redis_lock,
)
from pycommon.config import RedisSettings

pytestmark = pytest.mark.integration


# --- Lua scripts ----------------------------------------------------------


async def test_fixed_window_script_runs_on_real_redis(redis_client: Redis) -> None:
    """register_script sends the body once then uses EVALSHA. A script that is
    subtly invalid Lua, or returns a shape the parser does not expect, fails
    here and nowhere else."""
    limiter = RedisRateLimiter(redis_client)

    results = [await limiter.hit("k", times=3, seconds=60) for _ in range(4)]

    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0]
    assert results[0].reset_after_seconds > 0
    assert not any(r.degraded for r in results)


async def test_fixed_window_sets_ttl_only_on_first_hit(redis_client: Redis) -> None:
    """The window must not slide forward on every request. If PEXPIRE ran each
    time, a steady stream of traffic would keep the key alive forever and the
    window would never reset."""
    limiter = RedisRateLimiter(redis_client)

    await limiter.hit("k", times=100, seconds=2)
    await asyncio.sleep(0.5)
    await limiter.hit("k", times=100, seconds=2)

    keys = await redis_client.keys("*")
    ttl_ms = await redis_client.pttl(keys[0])
    assert ttl_ms <= 1600  # still counting down from the first hit, not reset to 2000


async def test_fixed_window_actually_expires(redis_client: Redis) -> None:
    """Real expiry, not a simulated clock."""
    limiter = RedisRateLimiter(redis_client)

    assert (await limiter.hit("k", times=1, seconds=1)).allowed is True
    assert (await limiter.hit("k", times=1, seconds=1)).allowed is False
    await asyncio.sleep(1.2)
    assert (await limiter.hit("k", times=1, seconds=1)).allowed is True


async def test_sliding_window_uses_the_server_clock(redis_client: Redis) -> None:
    """Scores come from Redis TIME so instances with skewed clocks cannot corrupt
    a shared window — fakeredis has no server clock to disagree with."""
    limiter = RedisSlidingWindowRateLimiter(redis_client)

    await limiter.hit("k", times=5, seconds=60)
    keys = await redis_client.keys("*")
    scores = await redis_client.zrange(keys[0], 0, -1, withscores=True)

    now_ms = time.time() * 1000
    assert len(scores) == 1
    # Within a few seconds of the host clock: proves it is a real epoch
    # timestamp from the server, not a counter or the caller's value.
    assert abs(scores[0][1] - now_ms) < 5000


async def test_sliding_window_does_not_record_denied_requests(redis_client: Redis) -> None:
    """A client hammering a closed limit must not keep extending its own window —
    otherwise the block never lifts for as long as it keeps trying."""
    limiter = RedisSlidingWindowRateLimiter(redis_client)

    for _ in range(2):
        await limiter.hit("k", times=2, seconds=60)
    for _ in range(10):
        assert (await limiter.hit("k", times=2, seconds=60)).allowed is False

    keys = await redis_client.keys("*")
    assert await redis_client.zcard(keys[0]) == 2  # the denials were not stored


async def test_sliding_window_has_no_boundary_burst(redis_client: Redis) -> None:
    """The reason this limiter exists: a fixed window allows 2x the limit across
    a boundary. A sliding window must not."""
    limiter = RedisSlidingWindowRateLimiter(redis_client)

    for _ in range(3):
        assert (await limiter.hit("k", times=3, seconds=2)).allowed is True
    await asyncio.sleep(1.0)  # halfway through the window
    assert (await limiter.hit("k", times=3, seconds=2)).allowed is False


# --- distributed lock -----------------------------------------------------


async def test_lock_is_mutually_exclusive(redis_client: Redis) -> None:
    """Two coroutines, one lock, real Redis SET NX."""
    order: list[str] = []

    async def worker(name: str) -> None:
        async with redis_lock(redis_client, "job", ttl_seconds=5):
            order.append(f"{name}-in")
            await asyncio.sleep(0.15)
            order.append(f"{name}-out")

    await asyncio.gather(worker("a"), worker("b"))

    # Whoever went first must have finished before the other started.
    assert order[1].endswith("-out")
    assert order[0].split("-")[0] == order[1].split("-")[0]


async def test_lock_releases_on_exception(redis_client: Redis) -> None:
    """A holder that raises must not leave the key behind for its whole TTL."""
    with pytest.raises(RuntimeError):
        async with redis_lock(redis_client, "job", ttl_seconds=30):
            raise RuntimeError("boom")

    async with redis_lock(redis_client, "job", ttl_seconds=5, blocking_timeout=0.5):
        pass  # acquired immediately: the previous holder really did release


async def test_auto_extend_outlives_the_ttl(redis_client: Redis) -> None:
    """Work of unpredictable duration must not lose its lock mid-flight. With
    auto_extend the TTL is refreshed every ttl/2 — this holds a 1s lock for 2.5s
    and asserts nobody else could take it in the meantime."""
    stolen = False

    async def thief() -> None:
        nonlocal stolen
        await asyncio.sleep(1.5)
        with contextlib.suppress(LockAcquireError):
            async with redis_lock(redis_client, "job", ttl_seconds=1, blocking_timeout=0.2):
                stolen = True

    async def holder() -> None:
        async with redis_lock(redis_client, "job", ttl_seconds=1, auto_extend=True):
            await asyncio.sleep(2.5)

    await asyncio.gather(holder(), thief())
    assert stolen is False


# --- cache ----------------------------------------------------------------


async def test_cache_roundtrip_and_real_ttl(redis_client: Redis) -> None:
    cache = Cache(redis_client, namespace="t", ttl_seconds=1)
    await cache.set("k", {"a": 1})
    assert await cache.get("k") == {"a": 1}
    await asyncio.sleep(1.2)
    assert await cache.get("k") is None


async def test_stampede_protection_computes_once(redis_client: Redis) -> None:
    """The point of the feature: a popular key expiring under load must not send
    every concurrent caller to the database. Needs a real lock to mean anything."""
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.3)
        return 42

    cache = Cache(redis_client, namespace="t", ttl_seconds=30)
    results = await asyncio.gather(*(cache.get_or_set("hot", factory) for _ in range(8)))

    assert results == [42] * 8
    assert calls == 1


async def test_clear_only_touches_its_namespace(redis_client: Redis) -> None:
    """clear() scans by prefix; the hash tag keeps a namespace on one slot. A
    clear that reached other namespaces would be a very quiet outage."""
    a = Cache(redis_client, namespace="a", ttl_seconds=60)
    b = Cache(redis_client, namespace="b", ttl_seconds=60)
    await a.set("k", 1)
    await b.set("k", 2)

    await a.clear()

    assert await a.get("k") is None
    assert await b.get("k") == 2


# --- failure modes --------------------------------------------------------


async def test_rate_limiter_fails_open_when_redis_is_gone() -> None:
    """Fail-open is asserted elsewhere with a mock; here the connection genuinely
    refuses, which is what a real outage looks like."""
    import redis.asyncio as redis_asyncio

    dead: Redis = redis_asyncio.from_url(
        "redis://127.0.0.1:1/0", socket_connect_timeout=0.25, socket_timeout=0.25
    )
    try:
        result = await RedisRateLimiter(dead).hit("k", times=1, seconds=60)
        assert result.allowed is True
        assert result.degraded is True
    finally:
        await dead.aclose()


# --- idempotency ----------------------------------------------------------


async def test_idempotent_replay_against_real_redis(redis_client: Redis) -> None:
    """SET NX and a real TTL, which is the whole mechanism. fakeredis models the
    happy path; expiry and atomicity are what decide whether a duplicate order
    can slip through."""
    import httpx
    from fastapi import FastAPI

    from pycommon.http.middleware import IdempotencyMiddleware

    calls: list[int] = []
    app = FastAPI()

    @app.post("/orders")
    async def create() -> dict[str, int]:
        calls.append(1)
        return {"order": len(calls)}

    app.add_middleware(IdempotencyMiddleware, redis=redis_client, ttl_seconds=2)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        headers = {"Idempotency-Key": "k1"}
        first = await client.post("/orders", json={}, headers=headers)
        replay = await client.post("/orders", json={}, headers=headers)

        assert first.json() == replay.json() == {"order": 1}
        assert replay.headers["Idempotent-Replay"] == "true"
        assert len(calls) == 1

        # After the TTL the key is genuinely gone and the request runs again.
        await asyncio.sleep(2.2)
        after = await client.post("/orders", json={}, headers=headers)
        assert after.json() == {"order": 2}
        assert "Idempotent-Replay" not in after.headers


async def test_concurrent_keys_collapse_to_one_run_on_real_redis(redis_client: Redis) -> None:
    """Two requests racing for the same key: exactly one wins the SET NX and
    runs; the other must not start a second copy of the same operation."""
    import httpx
    from fastapi import FastAPI

    from pycommon.http.middleware import IdempotencyMiddleware

    calls: list[int] = []
    release = asyncio.Event()

    app = FastAPI()

    @app.post("/orders")
    async def create() -> dict[str, int]:
        calls.append(1)
        await release.wait()
        return {"order": 1}

    app.add_middleware(IdempotencyMiddleware, redis=redis_client)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        headers = {"Idempotency-Key": "race"}
        tasks = [
            asyncio.create_task(client.post("/orders", json={}, headers=headers)) for _ in range(5)
        ]
        await asyncio.sleep(0.1)
        release.set()
        results = await asyncio.gather(*tasks)

    assert len(calls) == 1
    assert sum(1 for r in results if r.status_code == 409) == 4


# --- the client factory ---------------------------------------------------


async def test_create_redis_builds_a_client_that_works(redis_url: str) -> None:
    """The library's own factory, against a real server.

    Every other test here builds its client with ``redis_asyncio.from_url``, so
    the pool ``create_redis`` actually hands to services -- keepalive, health
    checks, timeouts, ``retry_on_timeout`` -- was never once accepted by a real
    Redis. redis-py has changed the meaning of those arguments across releases,
    and a combination it rejects fails at connect time in production and nowhere
    in the suite.
    """
    settings = RedisSettings(url=redis_url, max_connections=3)
    client = create_redis(settings)

    try:
        assert await client.ping() is True
        await client.set("factory:probe", "ok")
        assert await client.get("factory:probe") == "ok"  # decoded, not bytes
    finally:
        await client.aclose()


async def test_create_redis_can_speak_bytes(redis_url: str) -> None:
    """``decode_responses=False`` is what anything storing pickled or compressed
    values needs, and it has to survive the pool the factory builds."""
    client = create_redis(RedisSettings(url=redis_url), decode_responses=False)

    try:
        await client.set("factory:raw", b"\x00\x01binary")
        assert await client.get("factory:raw") == b"\x00\x01binary"
    finally:
        await client.aclose()
