"""Repository + UnitOfWork behavior against an in-memory SQLite database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from unittest import mock

import pytest
from sqlalchemy import String
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from py_common.persistence import SqlAlchemyRepository, SqlAlchemyUnitOfWork, sqlalchemy_repository


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"

    item_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50))


class ItemRepository(SqlAlchemyRepository[Item, int]):
    model = Item


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


async def test_crud_with_non_uuid_custom_named_pk(session: AsyncSession) -> None:
    """PK is `item_id: int`, not `id: UUID` — repository must not care."""
    repo = ItemRepository(session)

    created = await repo.create(Item(name="widget"))
    assert created.item_id is not None

    fetched = await repo.get(created.item_id)
    assert fetched is not None and fetched.name == "widget"

    fetched.name = "gadget"
    updated = await repo.update(fetched)
    assert updated.name == "gadget"

    assert await repo.delete(created.item_id) is True
    assert await repo.get(created.item_id) is None


async def test_delete_missing_returns_false(session: AsyncSession) -> None:
    repo = ItemRepository(session)
    assert await repo.delete(9999) is False


async def test_get_list_pagination(session: AsyncSession) -> None:
    repo = ItemRepository(session)
    for i in range(5):
        await repo.create(Item(name=f"item-{i}"))

    page = await repo.get_list(limit=2, offset=2, order_by=Item.item_id)
    assert [i.name for i in page] == ["item-2", "item-3"]


async def test_uow_commits_on_success(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.session is not None
        uow.session.add(Item(name="committed"))

    async with factory() as check:
        repo = ItemRepository(check)
        items = await repo.get_list()
        assert [i.name for i in items] == ["committed"]


async def test_uow_rolls_back_on_error(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)

    with pytest.raises(RuntimeError, match="boom"):
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            uow.session.add(Item(name="doomed"))
            raise RuntimeError("boom")

    async with factory() as check:
        repo = ItemRepository(check)
        assert await repo.get_list() == []


async def test_query_logger_emits_structured_log() -> None:
    from structlog.testing import capture_logs

    from py_common.persistence.query_logging import install_query_logger

    engine = create_async_engine("sqlite+aiosqlite://")
    install_query_logger(engine, slow_query_threshold_ms=0.0)
    with capture_logs() as logs:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
    await engine.dispose()

    query_logs = [entry for entry in logs if entry.get("event") == "db_query"]
    assert query_logs
    entry = query_logs[0]
    assert "SELECT 1" in entry["db"]["statement"]
    assert isinstance(entry["duration_ms"], float)


async def test_query_logger_threshold_skips_fast_queries() -> None:
    from structlog.testing import capture_logs

    from py_common.persistence.query_logging import install_query_logger

    engine = create_async_engine("sqlite+aiosqlite://")
    install_query_logger(engine, slow_query_threshold_ms=60_000.0)
    with capture_logs() as logs:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
    await engine.dispose()

    assert [entry for entry in logs if entry.get("event") in {"db_query", "slow_query"}] == []


async def test_migration_lifespan_skips_when_disabled() -> None:
    from structlog.testing import capture_logs

    from py_common.config import DatabaseSettings
    from py_common.persistence import migration_lifespan_resource

    settings = DatabaseSettings(auto_migrate=False)
    resource = migration_lifespan_resource(settings)
    with capture_logs() as logs:
        await resource.startup()
    assert any(entry.get("event") == "migrations_skipped" for entry in logs)


async def test_query_logger_logs_failed_queries() -> None:
    """Failing queries are the ones worth seeing — they used to be logged not at all."""
    from structlog.testing import capture_logs

    from py_common.persistence.query_logging import install_query_logger

    engine = create_async_engine("sqlite+aiosqlite://")
    # High threshold: failures must be reported regardless of how fast they fail.
    install_query_logger(engine, slow_query_threshold_ms=60_000.0)
    with capture_logs() as logs:
        async with engine.connect() as conn:
            with pytest.raises(Exception, match="no such table"):
                await conn.exec_driver_sql("SELECT * FROM missing_table")
    await engine.dispose()

    failed = [entry for entry in logs if entry.get("event") == "db_query_failed"]
    assert len(failed) == 1
    assert "missing_table" in failed[0]["db"]["statement"]
    assert "no such table" in failed[0]["error"]
    assert isinstance(failed[0]["duration_ms"], float)


async def test_query_logger_does_not_leak_timings_on_failure() -> None:
    """Timing lives on the per-execution context, not on the pooled connection.

    A failed query never reaches after_cursor_execute, so the old
    connection-scoped stack grew by one entry per failure for the whole life of
    the connection and could mis-pair later measurements.
    """
    from py_common.persistence.query_logging import install_query_logger

    engine = create_async_engine("sqlite+aiosqlite://")
    install_query_logger(engine)
    async with engine.connect() as conn:
        for _ in range(5):
            with pytest.raises(Exception, match="no such table"):
                await conn.exec_driver_sql("SELECT * FROM missing_table")
        info = await conn.run_sync(lambda c: dict(c.info))
    await engine.dispose()

    assert info == {}


async def test_pk_column_is_resolved_once_per_model(session: AsyncSession) -> None:
    """Every get()/delete() used to re-inspect the mapper for a fixed answer."""
    from py_common.persistence.sqlalchemy_repository import SqlAlchemyRepository

    SqlAlchemyRepository._pk_columns.pop(Item, None)
    calls = 0
    original = sqlalchemy_inspect

    def counting_inspect(target: object) -> object:
        nonlocal calls
        calls += 1
        return original(target)

    with mock.patch.object(sqlalchemy_repository, "inspect", counting_inspect):
        repo = ItemRepository(session)
        created = await repo.create(Item(name="widget"))
        await repo.get(created.item_id)
        await repo.get(created.item_id)
        # A fresh repository per request is the normal case, and must not re-inspect.
        await ItemRepository(session).get(created.item_id)

    assert calls == 1


def test_driver_named_by_the_dsn_is_installed() -> None:
    """`uv add "py-common[persistence]"` must be enough to build an engine. The
    DSN hardcodes postgresql+asyncpg, so the extra owes the caller that driver."""
    from py_common.config import DatabaseSettings

    assert DatabaseSettings().async_dsn.startswith("postgresql+asyncpg://")
    import asyncpg  # noqa: F401


async def test_in_memory_repository_orders_like_the_real_one() -> None:
    from py_common.testing.fakes import InMemoryRepository

    @dataclass
    class Row:
        id: int
        group: str
        name: str

    repo: InMemoryRepository[Row, int] = InMemoryRepository()
    for row in (Row(1, "b", "z"), Row(2, "a", "y"), Row(3, "a", "x")):
        await repo.create(row)

    assert [r.id for r in await repo.get_list(order_by="name")] == [3, 2, 1]
    assert [r.id for r in await repo.get_list(order_by="-name")] == [1, 2, 3]
    # Mixed directions across keys: what a single tuple sort key cannot express.
    assert [r.id for r in await repo.get_list(order_by=["group", "-name"])] == [2, 3, 1]
    assert [r.id for r in await repo.get_list(order_by="name", limit=2, offset=1)] == [2, 1]


async def test_in_memory_repository_default_ordering() -> None:
    from py_common.testing.fakes import InMemoryRepository

    @dataclass
    class Row:
        id: int

    repo: InMemoryRepository[Row, int] = InMemoryRepository(default_order_by="-id")
    for row in (Row(1), Row(3), Row(2)):
        await repo.create(row)

    assert [r.id for r in await repo.get_list()] == [3, 2, 1]
    assert [r.id for r in await repo.get_list(order_by="id")] == [1, 2, 3]


async def test_in_memory_repository_rejects_a_column_expression() -> None:
    """Ignoring it would leave the page unsorted and the assertion meaningless."""
    from py_common.testing.fakes import InMemoryRepository

    repo: InMemoryRepository[Item, int] = InMemoryRepository(id_attr="item_id")
    with pytest.raises(TypeError, match="attribute name"):
        await repo.get_list(order_by=Item.item_id)


def test_pool_settings_reach_the_engine() -> None:
    """pool_recycle and pool_timeout only help if they arrive at the pool — a
    silently dropped kwarg looks identical to a correctly configured one until a
    proxy starts closing idle connections.

    Asserted on a real engine's pool rather than on the kwargs passed to
    create_async_engine: mocking that call proves only that we said the words.
    """
    from py_common.config import DatabaseSettings
    from py_common.persistence.engine import create_engine_and_sessionmaker

    settings = DatabaseSettings(
        pool_size=7,
        max_overflow=3,
        pool_recycle_seconds=120,
        pool_timeout_seconds=11.0,
        pool_pre_ping=True,
    )
    engine, _ = create_engine_and_sessionmaker(settings, instrument=False)
    pool = engine.pool
    assert pool.size() == 7
    assert pool._recycle == 120
    assert pool._timeout == 11.0
    assert pool._pre_ping is True


@pytest.mark.asyncio
async def test_database_lifespan_resource_connects_then_disposes() -> None:
    """Startup verifies connectivity rather than deferring the failure to the
    first request, and shutdown returns the pool's sockets."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from py_common.persistence.engine import database_lifespan_resource

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    resource = database_lifespan_resource(engine)

    assert resource.name == "database"
    pool_before = engine.pool
    await resource.startup()
    await resource.shutdown()

    # dispose() replaces the pool rather than poisoning the engine; a new pool
    # object is the observable proof the old connections were returned.
    assert engine.pool is not pool_before


def test_alembic_helpers_name_the_extra_when_alembic_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``py-common[persistence]`` does not install Alembic; ``[migrations]`` does.

    A bare ``ModuleNotFoundError: alembic`` leaves the reader guessing which
    extra they missed, so the guard says it.
    """
    import sys

    from py_common.config import DatabaseSettings
    from py_common.persistence import build_alembic_config

    monkeypatch.setitem(sys.modules, "alembic.config", None)

    with pytest.raises(ImportError, match=r"py-common\[migrations\]"):
        build_alembic_config(DatabaseSettings())
