"""Cache-aside for values and async functions, with stampede protection.

This caches *values*, not HTTP responses: it takes no ``Request`` and works the
same from a FastAPI route, a gRPC servicer, a Celery worker or a CLI job.
Services that want HTTP response caching (ETag / 304 / ``Cache-Control``) should
add a dedicated library at the service layer — see "Libraries we deliberately
don't vendor" in the README.

Both reads and writes fail **open**: if Redis is unreachable the factory runs
and its value is returned uncached. A cache that takes the application down is
worse than no cache at all.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, Protocol

from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError

from pycommon.cache.lock import LockAcquireError, redis_lock
from pycommon.logging import get_logger

logger = get_logger(__name__)

CACHE_KEY_PREFIX = "cache"


class Serializer(Protocol):
    """Converts values to and from the strings stored in Redis."""

    def dumps(self, value: Any) -> str: ...

    def loads(self, raw: str) -> Any: ...


class JsonSerializer:
    """Stdlib JSON — handles dict, list, str, int, float, bool and None.

    Anything else (Pydantic models, ``datetime``, ``UUID``, ``Decimal``, enums)
    needs :func:`pydantic_serializer` or a serializer of your own. Silently
    coercing them to strings would round-trip a model into a dict and only fail
    much later, at the call site.
    """

    def dumps(self, value: Any) -> str:
        return json.dumps(value, separators=(",", ":"))

    def loads(self, raw: str) -> Any:
        return json.loads(raw)


def pydantic_serializer[ModelT: BaseModel](model: type[ModelT]) -> Serializer:
    """Serializer that round-trips a Pydantic model through its own JSON schema."""

    class _PydanticSerializer:
        def dumps(self, value: Any) -> str:
            if isinstance(value, model):
                return value.model_dump_json()
            raise TypeError(f"Expected {model.__name__}, got {type(value).__name__}")

        def loads(self, raw: str) -> Any:
            return model.model_validate_json(raw)

    return _PydanticSerializer()


class Cache:
    """Cache-aside over Redis for a single namespace.

    ``ttl_seconds`` is required rather than defaulted: an entry with no
    expiry is a leak plus permanently stale data if an invalidation is ever
    missed. Pass ``None`` explicitly for entries you will always invalidate
    by hand.

    Usage::

        cache = Cache(redis, namespace="products", ttl_seconds=300)
        product = await cache.get_or_set(
            product_id,
            lambda: repository.get(product_id),
        )
        await cache.delete(product_id)      # after a write
        await cache.clear()                 # whole namespace
    """

    def __init__(
        self,
        client: Redis,
        *,
        namespace: str,
        ttl_seconds: float | None,
        serializer: Serializer | None = None,
        stampede_protection: bool = True,
        lock_timeout_seconds: float = 10.0,
    ) -> None:
        self.client = client
        self.namespace = namespace
        self.ttl_seconds = ttl_seconds
        self.serializer: Serializer = serializer or JsonSerializer()
        self.stampede_protection = stampede_protection
        self.lock_timeout_seconds = lock_timeout_seconds

    def build_key(self, key: str) -> str:
        # Braces make the namespace a Redis Cluster hash tag, so every key in
        # one namespace lands on the same slot and clear() can scan a single node.
        return f"{CACHE_KEY_PREFIX}:{{{self.namespace}}}:{key}"

    async def get(self, key: str) -> Any | None:
        """Return the cached value, or ``None`` on miss.

        A stored ``None`` is indistinguishable from a miss here; use
        :meth:`get_or_set` when that difference matters.
        """
        try:
            raw = await self.client.get(self.build_key(key))
        except RedisError:
            logger.warning("cache_unavailable", operation="get", namespace=self.namespace)
            return None
        if raw is None:
            return None
        return self._decode(key, raw)

    async def set(self, key: str, value: Any, *, ttl_seconds: float | None = -1.0) -> None:
        """Store ``value``. ``ttl_seconds`` defaults to the cache's own TTL."""
        ttl = self.ttl_seconds if ttl_seconds == -1.0 else ttl_seconds
        try:
            raw = self.serializer.dumps(value)
        except TypeError, ValueError:
            # Never fail the caller because a value would not serialize — the
            # cache is an optimisation, the value it was given is still good.
            logger.exception("cache_serialize_failed", namespace=self.namespace, key=key)
            return
        try:
            px = int(ttl * 1000) if ttl is not None else None
            await self.client.set(self.build_key(key), raw, px=px)
        except RedisError:
            logger.warning("cache_unavailable", operation="set", namespace=self.namespace)

    async def delete(self, key: str) -> None:
        try:
            await self.client.delete(self.build_key(key))
        except RedisError:
            logger.warning("cache_unavailable", operation="delete", namespace=self.namespace)

    async def clear(self) -> int:
        """Drop every key in this namespace. Returns how many were removed."""
        pattern = self.build_key("*")
        removed = 0
        try:
            async for found in self.client.scan_iter(match=pattern, count=500):
                removed += await self.client.delete(found)
        except RedisError:
            logger.warning("cache_unavailable", operation="clear", namespace=self.namespace)
        return removed

    async def get_or_set[T](
        self,
        key: str,
        factory: Callable[[], Awaitable[T]],
        *,
        ttl_seconds: float | None = -1.0,
    ) -> T:
        """Return the cached value, computing and storing it on a miss.

        With ``stampede_protection`` (the default), only one caller computes a
        given key at a time; the rest wait briefly and read the value it stored.
        Without it, a popular key expiring under load sends every concurrent
        request to the database at once.
        """
        cache_key = self.build_key(key)
        hit, value = await self._lookup(key, cache_key)
        if hit:
            return value  # type: ignore[no-any-return]

        if not self.stampede_protection:
            return await self._compute_and_store(key, factory, ttl_seconds)

        held = False
        try:
            async with redis_lock(
                self.client,
                f"{CACHE_KEY_PREFIX}:{self.namespace}:{key}",
                ttl_seconds=self.lock_timeout_seconds,
                blocking_timeout=self.lock_timeout_seconds,
            ):
                held = True
                # The holder we queued behind has probably just filled it.
                hit, value = await self._lookup(key, cache_key)
                if hit:
                    return value  # type: ignore[no-any-return]
                return await self._compute_and_store(key, factory, ttl_seconds)
        except LockAcquireError:
            # Waited too long for the lock. Duplicated work beats a failed
            # request, so fall through and compute without it.
            logger.warning("cache_lock_timeout", namespace=self.namespace, key=key)
        except RedisError:
            if held:
                # The lock was ours; this came from the factory, not from
                # locking. Retrying would run the factory a second time.
                raise
            logger.warning("cache_unavailable", operation="lock", namespace=self.namespace)

        return await self._compute_and_store(key, factory, ttl_seconds)

    async def _lookup(self, key: str, cache_key: str) -> tuple[bool, Any]:
        """Return ``(hit, value)`` — distinguishing a stored ``None`` from a miss."""
        try:
            raw = await self.client.get(cache_key)
        except RedisError:
            logger.warning("cache_unavailable", operation="get", namespace=self.namespace)
            return False, None
        if raw is None:
            return False, None
        decoded = self._decode(key, raw)
        return decoded is not _DECODE_FAILED, (None if decoded is _DECODE_FAILED else decoded)

    def _decode(self, key: str, raw: Any) -> Any:
        try:
            return self.serializer.loads(raw if isinstance(raw, str) else raw.decode())
        except Exception:
            # Poisoned or stale-format entry — treat as a miss rather than
            # failing every request until the TTL expires.
            logger.warning("cache_deserialize_failed", namespace=self.namespace, key=key)
            return _DECODE_FAILED

    async def _compute_and_store[T](
        self,
        key: str,
        factory: Callable[[], Awaitable[T]],
        ttl_seconds: float | None,
    ) -> T:
        value = await factory()
        await self.set(key, value, ttl_seconds=ttl_seconds)
        return value


class _DecodeFailed:
    __slots__ = ()


_DECODE_FAILED = _DecodeFailed()


def default_key_builder(
    fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> str:
    """Key from the function's qualified name plus a digest of its arguments.

    The name stays readable in ``redis-cli --scan`` output; the digest keeps
    keys bounded no matter how large the arguments are.
    """
    payload = f"{args!r}:{sorted(kwargs.items())!r}"
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"{fn.__qualname__}:{digest}"


def cached[**P, T](
    client: Redis,
    *,
    namespace: str,
    ttl_seconds: float | None,
    serializer: Serializer | None = None,
    key_builder: Callable[[Callable[..., Any], tuple[Any, ...], dict[str, Any]], str] | None = None,
    stampede_protection: bool = True,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Cache an async function's return value.

    Usage::

        @cached(redis, namespace="products", ttl_seconds=300)
        async def get_product(product_id: str) -> dict:
            return await repository.get(product_id)

        await get_product.invalidate("abc-123")

    The wrapper exposes ``cache`` (the underlying :class:`Cache`) and
    ``invalidate(*args, **kwargs)``, which drops the entry for exactly the
    arguments you would have called it with.
    """
    build_key = key_builder or default_key_builder

    def decorator(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        cache = Cache(
            client,
            namespace=namespace,
            ttl_seconds=ttl_seconds,
            serializer=serializer,
            stampede_protection=stampede_protection,
        )

        @wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            key = build_key(fn, args, kwargs)
            return await cache.get_or_set(key, lambda: fn(*args, **kwargs))

        async def invalidate(*args: P.args, **kwargs: P.kwargs) -> None:
            await cache.delete(build_key(fn, args, kwargs))

        wrapper.cache = cache  # type: ignore[attr-defined]
        wrapper.invalidate = invalidate  # type: ignore[attr-defined]
        return wrapper

    return decorator
