"""What SQLite cannot prove: real types, real constraints, real timeouts, real pooling."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import structlog
from sqlalchemy import Index, MetaData, UniqueConstraint, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from py_common.persistence import (
    SqlAlchemyRepository,
    SqlAlchemyUnitOfWork,
    install_query_logger,
    paginate_cursor,
)
from py_common.persistence.base import NAMING_CONVENTION
from py_common.persistence.mixins import SoftDeleteMixin, TimestampMixin, UUIDv7PrimaryKeyMixin

pytestmark = pytest.mark.integration


class Base(DeclarativeBase):
    # A private MetaData carrying the library's convention: the shared one is
    # module-global, and registering a test table on it would leak into any
    # other test that reflects or creates from it.
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Widget(Base, UUIDv7PrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "widgets_it"
    sku: Mapped[str] = mapped_column()
    name: Mapped[str] = mapped_column()

    __table_args__ = (
        UniqueConstraint("sku"),
        Index("ix_widgets_it_name", "name"),
    )


class WidgetRepo(SqlAlchemyRepository[Widget, uuid.UUID]):
    model = Widget


@pytest.fixture
async def schema(pg_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    async with pg_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield pg_engine
    async with pg_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(schema: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(schema, expire_on_commit=False)
    async with maker() as s:
        yield s


# --- types and DDL --------------------------------------------------------


async def test_uuid7_primary_key_round_trips_through_asyncpg(session: AsyncSession) -> None:
    """SQLite stores UUIDs as CHAR(32) and coerces freely; Postgres has a real
    uuid type and asyncpg refuses to guess. This is where a type mismatch shows."""
    w = Widget(sku="s1", name="one")
    session.add(w)
    await session.commit()

    fetched = (await session.execute(select(Widget).where(Widget.id == w.id))).scalar_one()
    assert isinstance(fetched.id, uuid.UUID)
    assert fetched.id.version == 7


async def test_timestamps_are_timezone_aware_from_the_server(session: AsyncSession) -> None:
    """TIMESTAMP WITH TIME ZONE, defaulted by the database clock. SQLite has no
    real timezone-aware type, so this claim was untested until now."""
    w = Widget(sku="s1", name="one")
    session.add(w)
    await session.commit()

    assert w.created_at.tzinfo is not None
    db_now = (await session.execute(text("SELECT now()"))).scalar_one()
    assert abs((db_now - w.created_at).total_seconds()) < 60


async def test_updated_at_moves_on_update_but_created_at_does_not(session: AsyncSession) -> None:
    w = Widget(sku="s1", name="one")
    session.add(w)
    await session.commit()
    created, first_update = w.created_at, w.updated_at

    await asyncio.sleep(0.05)
    w.name = "renamed"
    await session.commit()
    await session.refresh(w)

    assert w.created_at == created
    assert w.updated_at > first_update


async def test_naming_convention_reaches_real_ddl(session: AsyncSession) -> None:
    """The convention exists so Alembic autogenerate produces stable, explicit
    constraint names. Only the database can confirm what was actually created."""
    rows = (
        (
            await session.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'widgets_it'::regclass ORDER BY conname"
                )
            )
        )
        .scalars()
        .all()
    )

    assert "pk_widgets_it" in rows
    assert "uq_widgets_it_sku" in rows


# --- failure logging ------------------------------------------------------


def _capture_logs() -> list[dict]:
    events: list[dict] = []

    def sink(logger, method_name, event_dict):  # type: ignore[no-untyped-def]
        events.append({"event": event_dict.get("event"), **event_dict})
        raise structlog.DropEvent

    structlog.configure(processors=[sink])
    return events


async def test_constraint_violation_is_logged(schema: AsyncEngine) -> None:
    """The handle_error listener exists because deadlocks, statement timeouts and
    constraint violations were previously logged nowhere at all. SQLite could not
    produce any of them in a form worth asserting on."""
    install_query_logger(schema, log=structlog.get_logger("t"))
    events = _capture_logs()
    try:
        maker = async_sessionmaker(schema, expire_on_commit=False)
        async with maker() as s:
            s.add_all([Widget(sku="dup", name="a"), Widget(sku="dup", name="b")])
            with pytest.raises(IntegrityError):
                await s.commit()
    finally:
        structlog.reset_defaults()

    failures = [e for e in events if e["event"] == "db_query_failed"]
    assert failures, "a unique-violation must produce a db_query_failed line"
    assert "duplicate key" in failures[0]["error"]
    assert "INSERT INTO widgets_it" in failures[0]["db"]["statement"]


async def test_statement_timeout_is_logged(schema: AsyncEngine) -> None:
    """A statement timeout is the canonical 'query that never finishes' incident.
    It has no SQLite equivalent, so nothing covered this path before."""
    install_query_logger(schema, log=structlog.get_logger("t"))
    events = _capture_logs()
    try:
        maker = async_sessionmaker(schema, expire_on_commit=False)
        async with maker() as s:
            await s.execute(text("SET statement_timeout = '100ms'"))
            with pytest.raises(DBAPIError):
                await s.execute(text("SELECT pg_sleep(2)"))
    finally:
        structlog.reset_defaults()

    failures = [e for e in events if e["event"] == "db_query_failed"]
    assert failures
    assert "timeout" in failures[0]["error"].lower()


async def test_successful_queries_are_still_logged(schema: AsyncEngine) -> None:
    install_query_logger(schema, log=structlog.get_logger("t"))
    events = _capture_logs()
    try:
        maker = async_sessionmaker(schema, expire_on_commit=False)
        async with maker() as s:
            await s.execute(select(Widget))
    finally:
        structlog.reset_defaults()

    assert any(e["event"] == "db_query" for e in events)


# --- transactions ---------------------------------------------------------


async def test_unit_of_work_rolls_back_on_exception(schema: AsyncEngine) -> None:
    """Real MVCC, not SQLite's file locking."""
    maker = async_sessionmaker(schema, expire_on_commit=False)

    with pytest.raises(RuntimeError):
        async with SqlAlchemyUnitOfWork(maker) as uow:
            assert uow.session is not None
            uow.session.add(Widget(sku="rollback", name="x"))
            await uow.session.flush()
            raise RuntimeError("boom")

    async with maker() as s:
        found = (await s.execute(select(Widget).where(Widget.sku == "rollback"))).scalars().all()
    assert found == []


async def test_unit_of_work_commits_on_clean_exit(schema: AsyncEngine) -> None:
    maker = async_sessionmaker(schema, expire_on_commit=False)

    async with SqlAlchemyUnitOfWork(maker) as uow:
        assert uow.session is not None
        uow.session.add(Widget(sku="kept", name="x"))

    async with maker() as s:
        found = (await s.execute(select(Widget).where(Widget.sku == "kept"))).scalars().all()
    assert len(found) == 1


async def test_uncommitted_writes_are_invisible_to_another_session(schema: AsyncEngine) -> None:
    """Read-committed isolation, which SQLite's single-writer model cannot show."""
    maker = async_sessionmaker(schema, expire_on_commit=False)

    async with maker() as writer, maker() as reader:
        writer.add(Widget(sku="pending", name="x"))
        await writer.flush()  # sent, not committed

        seen = (await reader.execute(select(Widget).where(Widget.sku == "pending"))).scalars().all()
        assert seen == []

        await writer.commit()
        seen = (await reader.execute(select(Widget).where(Widget.sku == "pending"))).scalars().all()
        assert len(seen) == 1


# --- repository and pagination -------------------------------------------


async def test_repository_crud_against_postgres(session: AsyncSession) -> None:
    repo = WidgetRepo(session)
    created = await repo.create(Widget(sku="s1", name="one"))
    await session.commit()

    assert await repo.get(created.id) is not None

    await repo.delete(created.id)
    await session.commit()
    assert await repo.get(created.id) is None


async def test_cursor_pagination_with_a_real_uuid_column(session: AsyncSession) -> None:
    """The cursor is JSON, so the key travels as a string. SQLite accepts the
    string back; Postgres does not, and asyncpg raises rather than coercing —
    this is the case _coerce_key exists for, and the only place it is real."""
    session.add_all([Widget(sku=f"s{i:02d}", name=f"n{i}") for i in range(12)])
    await session.commit()

    seen: list[uuid.UUID] = []
    cursor: str | None = None
    while True:
        page = await paginate_cursor(
            session, select(Widget), key_column=Widget.id, limit=5, cursor=cursor
        )
        seen.extend(w.id for w in page.items)
        cursor = page.meta.next_cursor
        if not cursor:
            break

    assert len(seen) == 12
    assert seen == sorted(seen)
    assert len(set(seen)) == 12


# --- pooling --------------------------------------------------------------


async def test_pre_ping_recovers_a_connection_the_server_killed(pg_engine: AsyncEngine) -> None:
    """The failure pool_pre_ping and pool_recycle exist for: something outside
    the app closed a pooled connection without telling the pool. Reproduced here
    with pg_terminate_backend, which has no SQLite analogue."""
    from sqlalchemy.ext.asyncio import create_async_engine

    dsn = str(pg_engine.url.render_as_string(hide_password=False))
    engine = create_async_engine(dsn, pool_size=1, max_overflow=0, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            pid = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()

        # Kill it from another connection, exactly as a proxy or admin would.
        async with pg_engine.connect() as killer:
            await killer.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})

        # Without pre_ping this checkout hands back the dead socket and raises.
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()
