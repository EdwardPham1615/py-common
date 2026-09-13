"""Integration tests against real backing services.

Each group is skipped unless its URL is in the environment, so the default
`pytest` run stays offline and fast. CI provides both as service containers::

    REDIS_TEST_URL=redis://localhost:6379/15 \\
    POSTGRES_TEST_DSN=postgresql+asyncpg://user:pw@localhost:5432/db \\
        uv run pytest tests/integration

URLs come from the environment rather than constants because a developer's
services may require credentials, and those do not belong in the tree.

These exist because the rest of the suite runs on substitutes that do not model
what the code depends on. ``fakeredis`` does not faithfully execute Lua, has no
server-side ``TIME``, and does not really expire keys. SQLite has no statement
timeout, no advisory locks, no ``ON CONFLICT ON CONSTRAINT``, and coerces types
that asyncpg rejects outright. A green run against either proves the Python is
coherent, not that it works against the database the service actually runs.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import redis.asyncio as redis_asyncio
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool


@pytest.fixture
def redis_url() -> str:
    """The URL itself, for tests that build their own client.

    Same skip rule as :func:`redis_client` -- per-fixture, never a
    module-level ``pytestmark`` in this file, which pytest ignores here.
    """
    url = os.getenv("REDIS_TEST_URL")
    if not url:
        pytest.skip("REDIS_TEST_URL is not set; skipping real-Redis integration tests")
    return url


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """A client on a flushed database, or a skip when no Redis was provided.

    The skip lives in the fixture rather than a module-level ``pytestmark``
    because a ``pytestmark`` in conftest.py is silently ignored — the tests then
    run and fail on a null URL instead of skipping.

    Flushes on entry rather than exit so a failed run leaves its keys behind to
    inspect.
    """
    url = os.getenv("REDIS_TEST_URL")
    if not url:
        pytest.skip("REDIS_TEST_URL is not set; skipping real-Redis integration tests")
    client: Redis = redis_asyncio.from_url(url, decode_responses=False)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def pg_engine() -> AsyncIterator[AsyncEngine]:
    """An engine on a real Postgres, or a skip when none was provided."""
    dsn = os.getenv("POSTGRES_TEST_DSN")
    if not dsn:
        pytest.skip("POSTGRES_TEST_DSN is not set; skipping real-Postgres integration tests")
    engine = create_async_engine(dsn, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def pg_dsn_sync() -> str:
    """The sync (psycopg) DSN, for Alembic — it runs migrations synchronously."""
    dsn = os.getenv("POSTGRES_TEST_DSN")
    if not dsn:
        pytest.skip("POSTGRES_TEST_DSN is not set; skipping real-Postgres integration tests")
    return dsn.replace("+asyncpg", "+psycopg")


@pytest.fixture
def otlp_endpoint() -> str:
    """OTLP gRPC endpoint of a collector that can be queried back (Jaeger)."""
    endpoint = os.getenv("OTLP_TEST_ENDPOINT")
    if not endpoint:
        pytest.skip("OTLP_TEST_ENDPOINT is not set; skipping collector integration tests")
    return endpoint


@pytest.fixture
def jaeger_query_url() -> str:
    url = os.getenv("JAEGER_QUERY_URL")
    if not url:
        pytest.skip("JAEGER_QUERY_URL is not set; skipping collector integration tests")
    return url.rstrip("/")


@pytest.fixture
def storage_settings() -> Any:
    """StorageSettings pointed at a real S3-compatible server (MinIO)."""
    endpoint = os.getenv("S3_TEST_ENDPOINT")
    if not endpoint:
        pytest.skip("S3_TEST_ENDPOINT is not set; skipping object-storage integration tests")

    from pycommon.config import StorageSettings

    return StorageSettings(
        endpoint_url=endpoint,
        access_key=os.getenv("S3_TEST_ACCESS_KEY", "pycommon"),
        secret_key=os.getenv("S3_TEST_SECRET_KEY", "pycommon123"),
        bucket=f"it-{uuid.uuid4().hex[:12]}",
        # MinIO serves virtual-host style only with DNS wildcards; path style is
        # what any self-hosted S3 needs, and getting it wrong is the classic
        # "works against AWS, 404s against MinIO" failure.
        use_path_style=True,
    )
