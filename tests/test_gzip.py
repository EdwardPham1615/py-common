"""Response compression: what gets compressed, what deliberately does not."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from starlette.middleware.gzip import GZipMiddleware

from pycommon.config import BaseAppSettings, HttpSettings
from pycommon.http.middleware import apply_standard_middleware

# Comfortably above the threshold the tests configure, and repetitive enough
# that gzip actually shrinks it -- a random payload would not.
PAYLOAD = "compress me " * 200


@pytest.fixture
def redis() -> Iterator[Redis]:
    yield fakeredis.aioredis.FakeRedis()


def _settings(**http: object) -> BaseAppSettings:
    return BaseAppSettings(_env_file=None, http=HttpSettings(**http))  # type: ignore[arg-type]


def _app(*, redis: Redis | None = None, **http: object) -> FastAPI:
    app = FastAPI()

    @app.get("/big")
    async def big() -> dict[str, str]:
        return {"payload": PAYLOAD}

    @app.get("/small")
    async def small() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/events")
    async def events() -> StreamingResponse:
        async def stream() -> AsyncIterator[bytes]:
            for i in range(50):
                yield f"data: {PAYLOAD[:40]} {i}\n\n".encode()

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/orders")
    async def create_order() -> dict[str, str]:
        return {"payload": PAYLOAD}

    apply_standard_middleware(app, _settings(**http), redis=redis)
    return app


def test_not_installed_by_default() -> None:
    """Upgrading pycommon must not change a single byte for a service that did
    not ask for compression."""
    client = TestClient(_app())
    response = client.get("/big", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert response.json()["payload"] == PAYLOAD


def test_compresses_above_the_threshold() -> None:
    client = TestClient(_app(gzip_min_size=500))
    response = client.get("/big", headers={"Accept-Encoding": "gzip"})

    assert response.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in response.headers["vary"].lower()
    # httpx decodes for us, and only real gzip decodes -- so a body that still
    # reads correctly is the round-trip assertion.
    assert response.json()["payload"] == PAYLOAD
    # Content-Length is the compressed size, response.content the decoded one.
    assert int(response.headers["content-length"]) < len(response.content)


def test_small_response_is_left_alone() -> None:
    client = TestClient(_app(gzip_min_size=500))
    response = client.get("/small", headers={"Accept-Encoding": "gzip"})

    assert "content-encoding" not in response.headers
    assert response.json() == {"ok": "yes"}


def test_client_that_did_not_ask_is_left_alone() -> None:
    client = TestClient(_app(gzip_min_size=500))
    response = client.get("/big", headers={"Accept-Encoding": "identity"})

    assert "content-encoding" not in response.headers
    assert response.json()["payload"] == PAYLOAD


def test_server_sent_events_are_not_compressed() -> None:
    """Compressing an event stream buffers it, which defeats the point of one.

    Starlette excludes ``text/event-stream`` by default; this pins that, because
    losing it would turn every SSE endpoint in every consuming service into a
    stream that arrives in one lump, with nothing failing to say so.
    """
    client = TestClient(_app(gzip_min_size=500))
    with client.stream("GET", "/events", headers={"Accept-Encoding": "gzip"}) as response:
        assert "content-encoding" not in response.headers
        first = next(response.iter_lines())
    assert first.startswith("data: ")


def test_compressed_response_keeps_request_id_and_security_headers() -> None:
    """Compression sits inside those layers, so their headers must survive it."""
    client = TestClient(_app(gzip_min_size=500))
    response = client.get("/big", headers={"Accept-Encoding": "gzip"})

    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_idempotent_replay_is_compressed_per_request(redis: Redis) -> None:
    """The stored body must be the plain one.

    Compression sits outside :class:`IdempotencyMiddleware` precisely so a
    gzipped body never reaches the store. If it did, the replay would hand those
    bytes to a client that never sent ``Accept-Encoding: gzip`` and cannot
    decode them.
    """
    client = TestClient(_app(redis=redis, gzip_min_size=500))
    headers = {"Idempotency-Key": "k-1"}

    first = client.post("/orders", headers={**headers, "Accept-Encoding": "gzip"})
    assert first.headers["content-encoding"] == "gzip"
    assert first.json()["payload"] == PAYLOAD

    replay = client.post("/orders", headers={**headers, "Accept-Encoding": "identity"})
    assert replay.headers["Idempotent-Replay"] == "true"
    assert "content-encoding" not in replay.headers
    assert replay.json()["payload"] == PAYLOAD


def test_setting_reaches_the_middleware() -> None:
    app = _app(gzip_min_size=1234)
    installed = [m for m in app.user_middleware if m.cls is GZipMiddleware]

    assert len(installed) == 1
    assert installed[0].kwargs["minimum_size"] == 1234


def test_no_gzip_layer_when_unset() -> None:
    app = _app()
    assert not [m for m in app.user_middleware if m.cls is GZipMiddleware]
