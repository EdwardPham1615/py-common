"""What one worker process holds: an event loop, a Redis client, an optional database."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol

from py_common.cache import LockAcquireError, create_redis, redis_lock
from py_common.config import RedisSettings, WorkerSettings
from py_common.logging import get_logger

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from py_common.persistence import SqlAlchemyUnitOfWork

logger = get_logger(__name__)


class WorkerHost(Protocol):
    """The part of a service's settings a worker reads. ``BaseAppSettings`` subclasses
    that declare ``worker`` and ``redis`` satisfy it; ``postgres`` and ``otel`` are
    picked up when present."""

    worker: WorkerSettings
    redis: RedisSettings


class WorkerRuntime:
    """Process-wide resources of one worker process.

    Celery tasks are synchronous and the library's persistence, storage and cache are
    async, so each worker process owns **one event loop** and runs every task's
    coroutine on it. Async connections belong to the loop that opened them and cannot
    cross a ``fork()``, so this is built lazily, on the first task, inside the
    process that runs it -- never at import. Subclass it to add the service's own
    clients and extend :meth:`aclose` to release them.
    """

    def __init__(self, settings: WorkerHost) -> None:
        self.settings = settings
        self.loop = asyncio.new_event_loop()
        self.redis: Redis = create_redis(settings.redis)
        self.engine: Any = None
        self.session_factory: Any = None
        postgres = getattr(settings, "postgres", None)
        if postgres is not None:
            # Imported only for a service that has a database: the worker extra does
            # not bring SQLAlchemy.
            from py_common.persistence import create_engine_and_sessionmaker

            self.engine, self.session_factory = create_engine_and_sessionmaker(postgres)

    def run[T](self, awaitable: Awaitable[T]) -> T:
        """Run a coroutine to completion on this process's loop."""
        return self.loop.run_until_complete(awaitable)

    @asynccontextmanager
    async def uow(self) -> AsyncIterator[SqlAlchemyUnitOfWork]:
        """One unit of work per task, the way the API opens one per request."""
        if self.session_factory is None:
            raise RuntimeError("this worker has no database: settings declare no `postgres`")
        from py_common.persistence import SqlAlchemyUnitOfWork

        async with SqlAlchemyUnitOfWork(self.session_factory) as uow:
            yield uow

    async def aclose(self) -> None:
        await self.redis.aclose()
        if self.engine is not None:
            await self.engine.dispose()

    def close(self) -> None:
        try:
            self.run(self.aclose())
        finally:
            self.loop.close()


@asynccontextmanager
async def scheduled_lock(
    runtime: WorkerRuntime, name: str, *, ttl_seconds: float = 300.0
) -> AsyncIterator[bool]:
    """Yield whether this run holds the job's lock.

    Beat starts a scheduled job once, but a manual run can start the same job at the
    same moment; the second one sees ``False`` and should return. The lock extends
    itself while held, so a long job does not lose it, and is released on the way out
    whatever the body does.
    """
    async with AsyncExitStack() as stack:
        try:
            await stack.enter_async_context(
                redis_lock(
                    runtime.redis,
                    f"job:{name}",
                    ttl_seconds=ttl_seconds,
                    blocking=False,
                    auto_extend=True,
                )
            )
        except LockAcquireError:
            logger.info("job_skipped_already_running", job=name)
            held = False
        else:
            held = True
        yield held
