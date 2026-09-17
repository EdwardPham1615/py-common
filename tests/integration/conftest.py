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

import json
import os
import pathlib
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
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

    from py_common.config import StorageSettings

    return StorageSettings(
        endpoint_url=endpoint,
        access_key=os.getenv("S3_TEST_ACCESS_KEY", "py_common"),
        secret_key=os.getenv("S3_TEST_SECRET_KEY", "pycommon123"),
        bucket=f"it-{uuid.uuid4().hex[:12]}",
        # MinIO serves virtual-host style only with DNS wildcards; path style is
        # what any self-hosted S3 needs, and getting it wrong is the classic
        # "works against AWS, 404s against MinIO" failure.
        use_path_style=True,
    )


# --- Keycloak --------------------------------------------------------------

REALM_FILE = pathlib.Path(__file__).with_name("keycloak-realm.json")


@pytest.fixture(scope="session")
def keycloak_realm() -> dict[str, Any]:
    """The realm definition the running container was started from.

    Read rather than duplicated as constants: client ids, secrets and the realm
    name would otherwise live in two places, and the day they drift the symptom
    is an ``invalid_client`` that looks like a broken server.
    """
    return json.loads(REALM_FILE.read_text())  # type: ignore[no-any-return]


@pytest.fixture
def keycloak_url() -> str:
    url = os.getenv("KEYCLOAK_TEST_URL")
    if not url:
        pytest.skip("KEYCLOAK_TEST_URL is not set; skipping real-Keycloak integration tests")
    return url.rstrip("/")


def _client_secret(realm: dict[str, Any], client_id: str) -> str:
    for client in realm["clients"]:
        if client["clientId"] == client_id:
            return str(client["secret"])
    raise LookupError(f"{client_id} is not in {REALM_FILE.name}")


@pytest.fixture
def keycloak_settings(keycloak_url: str, keycloak_realm: dict[str, Any]) -> Any:
    """Settings for the client that *does* carry a dedicated audience mapper."""
    from py_common.config import KeycloakSettings

    return KeycloakSettings(
        server_url=keycloak_url,
        realm=keycloak_realm["realm"],
        client_id="pycommon-api",
        client_secret=_client_secret(keycloak_realm, "pycommon-api"),
    )


@pytest.fixture
def keycloak_settings_noaud(keycloak_url: str, keycloak_realm: dict[str, Any]) -> Any:
    """Settings for the control client, which has no audience mapper."""
    from py_common.config import KeycloakSettings

    return KeycloakSettings(
        server_url=keycloak_url,
        realm=keycloak_realm["realm"],
        client_id="pycommon-api-noaud",
        client_secret=_client_secret(keycloak_realm, "pycommon-api-noaud"),
    )


@pytest.fixture
def keycloak_token(
    keycloak_url: str, keycloak_realm: dict[str, Any]
) -> Callable[..., dict[str, Any]]:
    """Ask the real server for a token, and hand back the whole response.

    Deliberately plain ``httpx`` rather than
    :class:`~py_common.security.ClientCredentialsTokenProvider`: that provider is
    one of the things under test here, so using it to set tests up would let a
    bug in it hide behind itself.
    """
    realm = keycloak_realm["realm"]

    def fetch(client_id: str, *, user: str | None = None) -> dict[str, Any]:
        form = {
            "client_id": client_id,
            "client_secret": _client_secret(keycloak_realm, client_id),
        }
        if user is None:
            form["grant_type"] = "client_credentials"
        else:
            form |= {"grant_type": "password", "username": user, "password": f"{user}-password"}

        response = httpx.post(
            f"{keycloak_url}/realms/{realm}/protocol/openid-connect/token",
            data=form,
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    return fetch
