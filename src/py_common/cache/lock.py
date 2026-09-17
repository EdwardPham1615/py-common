"""Distributed lock on Redis with optional auto-extend.

Built on redis-py's atomic ``Lock`` (token-checked release/extend via Lua),
with an optional background task that keeps extending the TTL while the
protected block is still running.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis
from redis.asyncio.lock import Lock
from redis.exceptions import LockError, RedisError

from py_common.logging import get_logger

logger = get_logger(__name__)

LOCK_KEY_PREFIX = "lock:"


class LockAcquireError(Exception):
    """Raised when the lock could not be acquired within ``blocking_timeout``."""


async def _auto_extend_loop(lock: Lock, ttl_seconds: float, key: str) -> None:
    """Keep extending the lock TTL until cancelled.

    Never lets an error propagate: an exception escaping this task would be
    re-raised when :func:`redis_lock` reaps it, skipping ``lock.release()`` and
    masking whatever the guarded block was already raising.
    """
    interval = ttl_seconds / 2
    while True:
        await asyncio.sleep(interval)
        try:
            await lock.extend(ttl_seconds, replace_ttl=True)
        except LockError:
            logger.warning("lock_extend_failed", key=key)
            return
        except Exception:
            # Redis unreachable, pool exhausted, ... — stop extending and let the
            # TTL run out rather than taking the caller down with us.
            logger.exception("lock_extend_error", key=key)
            return


async def _stop_extend_task(task: asyncio.Task[None] | None) -> None:
    """Cancel the auto-extend task and swallow its outcome.

    Anything raised while reaping would propagate out of :func:`redis_lock`'s
    ``finally`` block, which is precisely how the lock used to leak.
    """
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


@asynccontextmanager
async def redis_lock(
    client: Redis,
    key: str,
    *,
    ttl_seconds: float = 30.0,
    blocking: bool = True,
    blocking_timeout: float | None = 10.0,
    auto_extend: bool = False,
) -> AsyncIterator[None]:
    """Hold a distributed lock while the block runs.

    - ``ttl_seconds``: lock TTL (protects against dead holders).
    - ``blocking`` / ``blocking_timeout``: how long to wait for acquisition;
      raises :class:`LockAcquireError` when it cannot be acquired.
    - ``auto_extend``: keep extending the TTL every ``ttl_seconds / 2``
      while the block is still running (for work of unpredictable duration).

    Usage::

        async with redis_lock(redis, f"order:{order_id}", ttl_seconds=30):
            ...
    """
    lock = client.lock(
        f"{LOCK_KEY_PREFIX}{key}",
        timeout=ttl_seconds,
        blocking=blocking,
        blocking_timeout=blocking_timeout,
    )
    acquired = await lock.acquire()
    if not acquired:
        raise LockAcquireError(f"Could not acquire lock {key!r}")

    extend_task: asyncio.Task[None] | None = None
    if auto_extend:
        extend_task = asyncio.create_task(_auto_extend_loop(lock, ttl_seconds, key))

    try:
        yield
    finally:
        await _stop_extend_task(extend_task)
        try:
            await lock.release()
        except RedisError:
            # LockError (TTL expired before release — the work outlived the
            # lock) or the connection dropping. Either way the lock expires on
            # its own; failing the caller here would replace a completed unit of
            # work with an infrastructure error.
            logger.warning("lock_release_failed", key=key)
