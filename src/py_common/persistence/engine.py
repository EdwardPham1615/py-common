"""Async engine / sessionmaker factory wired from DatabaseSettings."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from py_common.config import DatabaseSettings
from py_common.logging import get_logger
from py_common.runtime.lifespan import LifespanResource

logger = get_logger(__name__)


def create_engine_and_sessionmaker(
    settings: DatabaseSettings,
    *,
    instrument: bool = True,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create an async engine + sessionmaker with standard pool settings.

    When ``instrument=True`` and the OTel SQLAlchemy instrumentation is
    installed, the engine is instrumented automatically.
    """
    engine = create_async_engine(
        settings.async_dsn,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_pre_ping=settings.pool_pre_ping,
        pool_recycle=settings.pool_recycle_seconds,
        pool_timeout=settings.pool_timeout_seconds,
        echo=settings.echo,
        echo_pool=settings.echo_pool,
    )
    if settings.log_queries:
        from py_common.persistence.query_logging import install_query_logger

        install_query_logger(
            engine,
            slow_query_threshold_ms=settings.slow_query_threshold_ms,
            log_params=settings.log_query_params,
            log=logger,
        )
    if instrument:
        try:
            from py_common.telemetry import instrument_sqlalchemy

            instrument_sqlalchemy(engine)
        except ImportError:
            logger.warning("sqlalchemy_instrumentation_unavailable")

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_factory


def database_lifespan_resource(
    engine: AsyncEngine,
    *,
    name: str = "database",
) -> LifespanResource:
    """LifespanResource that verifies connectivity on startup and disposes the pool on shutdown."""

    async def startup() -> None:
        async with engine.connect():
            pass

    async def shutdown() -> None:
        await engine.dispose()

    return LifespanResource(name=name, startup=startup, shutdown=shutdown)
