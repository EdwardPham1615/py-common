"""BodySizeLimitMiddleware: both checks, and why one alone is not enough."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from py_common.config import BaseAppSettings, HttpSettings
from py_common.errors import ErrorCode
from py_common.http.middleware import BodySizeLimitMiddleware, apply_standard_middleware


def _app(**kwargs: object) -> FastAPI:
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()
        return {"size": len(body)}

    app.add_middleware(BodySizeLimitMiddleware, **kwargs)
    return app


def test_small_body_passes() -> None:
    client = TestClient(_app(max_bytes=1000))
    resp = client.post("/echo", content=b"x" * 100)
    assert resp.status_code == 200
    assert resp.json() == {"size": 100}


def test_declared_content_length_is_rejected_before_reading() -> None:
    """The cheap path: an honest client announces the size and is turned away
    without a byte of body being buffered."""
    consumed = False

    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, str]:
        nonlocal consumed
        await request.body()
        consumed = True
        return {}

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=100)

    resp = TestClient(app).post("/echo", content=b"x" * 500)

    assert resp.status_code == 413
    assert consumed is False  # the handler was never entered


def test_rejection_is_problem_details() -> None:
    resp = TestClient(_app(max_bytes=100)).post("/echo", content=b"x" * 500)

    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["error_code"] == int(ErrorCode.PAYLOAD_TOO_LARGE)
    assert body["type"] == "/problems/payload-too-large"
    assert body["status"] == 413
    assert "100" in body["detail"]


async def test_streamed_body_without_content_length_is_still_capped() -> None:
    """The check that matters. Content-Length is optional under chunked transfer
    encoding and is in any case a claim by the caller, so a limit that trusts it
    stops honest clients and nobody else."""

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(10):
            yield b"x" * 50

    app = _app(max_bytes=100)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/echo", content=chunks())

    assert resp.status_code == 413
    assert "content-length" not in {k.lower() for k in resp.request.headers}


async def test_a_lying_content_length_does_not_get_through() -> None:
    """A caller who understates the size in the header is caught by the counter."""

    async def chunks() -> AsyncIterator[bytes]:
        yield b"x" * 500

    app = _app(max_bytes=100)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/echo", content=chunks(), headers={"content-length": "10"})

    assert resp.status_code == 413


def test_excluded_paths_take_any_size() -> None:
    """Exempting the few endpoints that legitimately take large bodies beats
    raising the global limit, which hands every other endpoint the allowance."""
    app = FastAPI()

    @app.post("/upload")
    async def upload(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=100, exclude_paths=["/upload"])

    resp = TestClient(app).post("/upload", content=b"x" * 5000)
    assert resp.status_code == 200
    assert resp.json() == {"size": 5000}


def test_get_without_a_body_is_untouched() -> None:
    app = FastAPI()

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        return {"ok": "yes"}

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=10)
    assert TestClient(app).get("/ok").status_code == 200


def test_non_positive_limit_is_rejected() -> None:
    """A limit of 0 would reject every request; accepting it silently would make
    the misconfiguration look like an outage."""
    with pytest.raises(ValueError, match="max_bytes must be"):
        BodySizeLimitMiddleware(FastAPI(), max_bytes=0)


def test_413_is_not_reported_as_a_server_error(capsys: pytest.CaptureFixture[str]) -> None:
    """The reason this sits innermost. BodyTooLarge is raised out of the
    handler's own read of the body; if a layer that renders unhandled exceptions
    caught it first, an oversized body would come back as a 500 and the client
    would be told the fault was ours."""
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    apply_standard_middleware(
        app,
        BaseAppSettings(_env_file=None, http=HttpSettings(max_body_bytes=100)),
    )

    resp = TestClient(app).post("/echo", content=b"x" * 500)

    assert resp.status_code == 413
    assert resp.json()["error_code"] == int(ErrorCode.PAYLOAD_TOO_LARGE)
    assert resp.headers["X-Request-ID"]
    assert resp.json()["request_id"] == resp.headers["X-Request-ID"]
