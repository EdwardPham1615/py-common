"""Idempotency-Key: replay, conflicts, scoping, and what is deliberately not stored."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import fakeredis.aioredis
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from py_common.http.middleware import IdempotencyMiddleware


@pytest.fixture
def redis() -> Iterator[Redis]:
    client = fakeredis.aioredis.FakeRedis()
    yield client


def _app(redis: Redis, **kwargs: object) -> tuple[FastAPI, list[int]]:
    calls: list[int] = []
    app = FastAPI()

    @app.post("/orders")
    async def create(request: Request) -> dict[str, int]:
        calls.append(1)
        return {"order": len(calls)}

    @app.post("/boom")
    async def boom() -> dict[str, str]:
        calls.append(1)
        raise RuntimeError("kaboom")

    @app.post("/refunds")
    async def refund() -> dict[str, str]:
        calls.append(1)
        return {"refund": "ok"}

    @app.get("/orders")
    async def list_orders() -> dict[str, int]:
        calls.append(1)
        return {"count": len(calls)}

    app.add_middleware(IdempotencyMiddleware, redis=redis, **kwargs)
    return app, calls


def test_repeat_with_same_key_replays_without_rerunning(redis: Redis) -> None:
    """The whole point. A client whose connection dropped cannot tell 'created'
    from 'not created'; the key is what lets it retry without making two."""
    app, calls = _app(redis)
    client = TestClient(app)
    headers = {"Idempotency-Key": "k1"}

    first = client.post("/orders", json={"item": "a"}, headers=headers)
    second = client.post("/orders", json={"item": "a"}, headers=headers)

    assert first.json() == second.json() == {"order": 1}
    assert second.headers["Idempotent-Replay"] == "true"
    assert len(calls) == 1  # the handler ran once


def test_no_key_means_no_idempotency(redis: Redis) -> None:
    """Not mandatory: requiring it would break every existing client the day it
    is switched on."""
    app, calls = _app(redis)
    client = TestClient(app)

    client.post("/orders", json={"item": "a"})
    client.post("/orders", json={"item": "a"})

    assert len(calls) == 2


def test_safe_methods_are_untouched(redis: Redis) -> None:
    app, calls = _app(redis)
    client = TestClient(app)
    headers = {"Idempotency-Key": "k1"}

    client.get("/orders", headers=headers)
    client.get("/orders", headers=headers)

    assert len(calls) == 2


def test_same_key_different_body_is_a_conflict(redis: Redis) -> None:
    """Answering with the first response would silently discard the second
    request, which is worse than refusing it."""
    app, _ = _app(redis)
    client = TestClient(app)
    headers = {"Idempotency-Key": "k1"}

    client.post("/orders", json={"item": "a"}, headers=headers)
    resp = client.post("/orders", json={"item": "DIFFERENT"}, headers=headers)

    assert resp.status_code == 409
    assert resp.json()["type"] == "/problems/idempotency"
    assert "different request body" in resp.json()["detail"]


def test_keys_are_scoped_per_endpoint(redis: Redis) -> None:
    """The same key on a different path is a different operation. Sharing one
    namespace would have a refund replay an order's response."""
    app, calls = _app(redis)
    client = TestClient(app)
    headers = {"Idempotency-Key": "shared"}

    order = client.post("/orders", json={}, headers=headers)
    refund = client.post("/refunds", json={}, headers=headers)

    assert len(calls) == 2  # both ran
    assert order.json() == {"order": 1}
    assert refund.json() == {"refund": "ok"}
    assert "Idempotent-Replay" not in refund.headers


def test_keys_are_scoped_per_caller(redis: Redis) -> None:
    """A key is chosen by the client, so two clients eventually pick the same
    one. Without scoping the second would be handed the first's response — a
    data leak, not a collision."""
    app, calls = _app(redis)
    headers = {"Idempotency-Key": "same-key"}

    a = TestClient(app, client=("10.0.0.1", 1234))
    b = TestClient(app, client=("10.0.0.2", 1234))

    first = a.post("/orders", json={"item": "a"}, headers=headers)
    second = b.post("/orders", json={"item": "a"}, headers=headers)

    assert len(calls) == 2
    assert "Idempotent-Replay" not in second.headers
    assert first.json() != second.json()


def test_server_errors_are_not_stored(redis: Redis) -> None:
    """A stored 500 would make the failure permanent for that key: every retry
    would replay the error instead of getting the second chance being asked for."""
    app, calls = _app(redis)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Idempotency-Key": "k1"}

    client.post("/boom", json={}, headers=headers)
    client.post("/boom", json={}, headers=headers)

    assert len(calls) == 2  # retried, not replayed


async def test_concurrent_requests_resolve_to_one_run(redis: Redis) -> None:
    """SET NX is what makes two simultaneous requests resolvable: one wins the
    reservation, the other finds a record rather than starting a second copy."""
    import httpx

    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    app = FastAPI()

    @app.post("/orders")
    async def create() -> dict[str, int]:
        calls.append(1)
        started.set()
        await release.wait()
        return {"order": 1}

    app.add_middleware(IdempotencyMiddleware, redis=redis)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        headers = {"Idempotency-Key": "k1"}
        first = asyncio.create_task(client.post("/orders", json={}, headers=headers))
        await started.wait()
        second = await client.post("/orders", json={}, headers=headers)
        release.set()
        await first

    assert second.status_code == 409
    assert "still in progress" in second.json()["detail"]
    assert len(calls) == 1


async def test_redis_outage_fails_closed_by_default() -> None:
    """Unlike the rate limiter and cache, this degrades the guarantee it exists
    to provide. A duplicate payment is worse than a rejection the client can
    safely retry — it is holding a key, after all."""

    class _Dead:
        async def set(self, *a: object, **k: object) -> None:
            raise RedisConnectionError("down")

        async def get(self, *a: object, **k: object) -> None:
            raise RedisConnectionError("down")

    app, calls = _app(_Dead())  # type: ignore[arg-type]
    resp = TestClient(app).post("/orders", json={}, headers={"Idempotency-Key": "k1"})

    assert resp.status_code == 503
    assert calls == []


async def test_fail_open_runs_the_handler_when_asked() -> None:
    class _Dead:
        async def set(self, *a: object, **k: object) -> None:
            raise RedisConnectionError("down")

        async def get(self, *a: object, **k: object) -> None:
            raise RedisConnectionError("down")

    app, calls = _app(_Dead(), fail_open=True)  # type: ignore[arg-type]
    resp = TestClient(app).post("/orders", json={}, headers={"Idempotency-Key": "k1"})

    assert resp.status_code == 200
    assert len(calls) == 1


def test_non_positive_ttl_is_rejected(redis: Redis) -> None:
    with pytest.raises(ValueError, match="ttl_seconds must be"):
        IdempotencyMiddleware(FastAPI(), redis, ttl_seconds=0)
