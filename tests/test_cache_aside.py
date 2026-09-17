"""Value cache: hits, misses, invalidation, stampede protection, fail-open."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from fakeredis import FakeAsyncRedis
from pydantic import BaseModel
from redis.exceptions import RedisError

from py_common.cache import Cache, cached, pydantic_serializer


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


async def test_get_or_set_computes_once_then_serves_from_cache(
    redis: FakeAsyncRedis,
) -> None:
    cache = Cache(redis, namespace="products", ttl_seconds=60)
    calls = 0

    async def factory() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"price": 42}

    assert await cache.get_or_set("p1", factory) == {"price": 42}
    assert await cache.get_or_set("p1", factory) == {"price": 42}
    assert calls == 1


async def test_cached_none_is_not_a_miss(redis: FakeAsyncRedis) -> None:
    """Negative caching only works if a stored None counts as a hit."""
    cache = Cache(redis, namespace="products", ttl_seconds=60)
    calls = 0

    async def factory() -> None:
        nonlocal calls
        calls += 1
        return None

    assert await cache.get_or_set("missing", factory) is None
    assert await cache.get_or_set("missing", factory) is None
    assert calls == 1, "the second call should have been served from cache"


async def test_delete_and_clear(redis: FakeAsyncRedis) -> None:
    cache = Cache(redis, namespace="products", ttl_seconds=60)
    await cache.set("a", 1)
    await cache.set("b", 2)
    assert await cache.get("a") == 1

    await cache.delete("a")
    assert await cache.get("a") is None
    assert await cache.get("b") == 2

    assert await cache.clear() == 1
    assert await cache.get("b") is None


async def test_ttl_is_applied(redis: FakeAsyncRedis) -> None:
    cache = Cache(redis, namespace="products", ttl_seconds=60)
    await cache.set("a", 1)
    assert 0 < await redis.ttl(cache.build_key("a")) <= 60

    await cache.set("forever", 1, ttl_seconds=None)
    assert await redis.ttl(cache.build_key("forever")) == -1


async def test_namespace_is_a_cluster_hash_tag(redis: FakeAsyncRedis) -> None:
    """Braces keep a namespace on one slot so clear() can scan a single node."""
    cache = Cache(redis, namespace="products", ttl_seconds=60)
    assert cache.build_key("p1") == "cache:{products}:p1"


async def test_pydantic_serializer_round_trips(redis: FakeAsyncRedis) -> None:
    class Product(BaseModel):
        id: str
        price: float

    cache = Cache(
        redis,
        namespace="products",
        ttl_seconds=60,
        serializer=pydantic_serializer(Product),
    )
    await cache.set("p1", Product(id="p1", price=9.5))

    restored = await cache.get("p1")
    assert isinstance(restored, Product), "must come back as the model, not a dict"
    assert restored.price == 9.5


async def test_stampede_protection_collapses_concurrent_misses(
    redis: FakeAsyncRedis,
) -> None:
    """A popular key expiring under load must not send every request to the DB."""
    cache = Cache(redis, namespace="products", ttl_seconds=60)
    calls = 0

    async def slow_factory() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return "value"

    results = await asyncio.gather(*(cache.get_or_set("hot", slow_factory) for _ in range(10)))

    assert results == ["value"] * 10
    assert calls == 1, f"expected one computation, got {calls}"


async def test_without_stampede_protection_every_caller_computes(
    redis: FakeAsyncRedis,
) -> None:
    cache = Cache(redis, namespace="products", ttl_seconds=60, stampede_protection=False)
    calls = 0

    async def slow_factory() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return "value"

    await asyncio.gather(*(cache.get_or_set("hot", slow_factory) for _ in range(5)))
    assert calls == 5


class _BrokenRedis:
    """Every command fails, as if Redis had gone away."""

    def lock(self, *args: object, **kwargs: object) -> object:
        # Redis.lock() is synchronous, so it raises here rather than on await.
        raise RedisError("connection lost")

    def __getattr__(self, name: str) -> object:
        async def _fail(*args: object, **kwargs: object) -> object:
            raise RedisError("connection lost")

        return _fail


async def test_cache_fails_open_when_redis_is_down() -> None:
    """A cache outage must degrade to a cache miss, never to a failed request."""
    cache = Cache(_BrokenRedis(), namespace="products", ttl_seconds=60)  # type: ignore[arg-type]
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        return "fresh"

    assert await cache.get_or_set("p1", factory) == "fresh"
    assert await cache.get("p1") is None
    await cache.set("p1", "x")
    await cache.delete("p1")
    assert calls == 1


async def test_poisoned_entry_is_treated_as_a_miss(redis: FakeAsyncRedis) -> None:
    """A stale-format entry must not fail every request until its TTL expires."""
    cache = Cache(redis, namespace="products", ttl_seconds=60)
    await redis.set(cache.build_key("p1"), "{not json")

    assert await cache.get_or_set("p1", lambda: _value("recomputed")) == "recomputed"


async def test_unserializable_value_is_returned_not_raised(redis: FakeAsyncRedis) -> None:
    """The cache is an optimisation; a value it cannot store is still a good value."""
    cache = Cache(redis, namespace="products", ttl_seconds=60)

    assert await cache.get_or_set("p1", lambda: _value(object())) is not None


async def _value[T](value: T) -> T:
    return value


async def test_cached_decorator_and_invalidate(redis: FakeAsyncRedis) -> None:
    calls = 0

    @cached(redis, namespace="products", ttl_seconds=60)
    async def get_product(product_id: str, *, currency: str = "USD") -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"id": product_id, "currency": currency}

    assert await get_product("p1") == {"id": "p1", "currency": "USD"}
    assert await get_product("p1") == {"id": "p1", "currency": "USD"}
    assert calls == 1

    # Different arguments are a different entry.
    await get_product("p2")
    assert calls == 2
    await get_product("p1", currency="EUR")
    assert calls == 3

    await get_product.invalidate("p1")
    await get_product("p1")
    assert calls == 4


async def test_cached_decorator_key_is_greppable(redis: FakeAsyncRedis) -> None:
    @cached(redis, namespace="products", ttl_seconds=60)
    async def get_product(product_id: str) -> str:
        return product_id

    await get_product("p1")
    keys = [
        k.decode() if isinstance(k, bytes) else k async for k in redis.scan_iter(match="cache:*")
    ]
    assert len(keys) == 1
    assert "get_product" in keys[0]


async def test_factory_error_is_not_retried_under_the_lock(redis: FakeAsyncRedis) -> None:
    """A factory that raises RedisError must not be run twice.

    The fail-open path exists for a Redis that is unreachable *for locking*.
    Once the lock is held, an error can only have come from the factory, and
    re-running it would repeat whatever side effects it already had.
    """
    cache = Cache(redis, namespace="products", ttl_seconds=60)
    calls = 0

    async def failing_factory() -> str:
        nonlocal calls
        calls += 1
        raise RedisError("the factory's own redis call failed")

    with pytest.raises(RedisError):
        await cache.get_or_set("p1", failing_factory)

    assert calls == 1
