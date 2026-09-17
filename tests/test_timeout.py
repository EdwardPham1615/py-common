"""TimeoutMiddleware: what it cuts off, what it deliberately does not."""

from __future__ import annotations

from collections.abc import Iterator

import anyio
import pytest
import structlog
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from py_common.config import BaseAppSettings, HttpSettings
from py_common.errors import ErrorCode
from py_common.http.middleware import TimeoutMiddleware, apply_standard_middleware


@pytest.fixture
def capture_logs() -> Iterator[list[dict]]:
    events: list[dict] = []

    def sink(logger, method_name, event_dict):  # type: ignore[no-untyped-def]
        events.append(dict(event_dict))
        raise structlog.DropEvent

    structlog.configure(processors=[sink])
    try:
        yield events
    finally:
        structlog.reset_defaults()


def test_fast_request_is_untouched() -> None:
    app = FastAPI()

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        return {"ok": "yes"}

    app.add_middleware(TimeoutMiddleware, seconds=5.0)
    resp = TestClient(app).get("/ok")
    assert resp.status_code == 200


def test_slow_request_gets_problem_details_504() -> None:
    app = FastAPI()

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        await anyio.sleep(5)
        return {"never": "returned"}

    app.add_middleware(TimeoutMiddleware, seconds=0.1)
    resp = TestClient(app).get("/slow")

    assert resp.status_code == 504
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["error_code"] == int(ErrorCode.TIMEOUT)
    assert body["type"] == "/problems/timeout"
    assert body["instance"] == "/slow"


def test_the_handler_is_cancelled_not_just_abandoned() -> None:
    """The point of the middleware. Answering 504 while the work continues would
    leave the database session checked out and the upstream call in flight —
    the same resource leak, minus the visibility."""
    finished = False

    app = FastAPI()

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        nonlocal finished
        await anyio.sleep(2)
        finished = True
        return {"done": "yes"}

    app.add_middleware(TimeoutMiddleware, seconds=0.1)
    assert TestClient(app).get("/slow").status_code == 504

    assert finished is False


def test_streaming_response_is_not_cut_off() -> None:
    """The timeout covers time-to-first-byte, not the whole response. A wall-clock
    limit on the complete body would kill exactly the endpoints that legitimately
    run long — SSE, large downloads, streamed exports."""
    app = FastAPI()

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def body() -> object:
            for i in range(5):
                await anyio.sleep(0.05)
                yield f"chunk{i}".encode()

        return StreamingResponse(body(), media_type="text/plain")

    # Total body time (~0.25s) far exceeds the timeout; first byte does not.
    app.add_middleware(TimeoutMiddleware, seconds=0.15)
    resp = TestClient(app).get("/stream")

    assert resp.status_code == 200
    assert resp.text == "chunk0chunk1chunk2chunk3chunk4"


def test_excluded_paths_are_not_timed_out() -> None:
    """Probes carry their own timeouts; a readiness check racing a middleware
    timeout produces two different answers to one question."""
    app = FastAPI()

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        await anyio.sleep(0.3)
        return {"status": "ok"}

    app.add_middleware(TimeoutMiddleware, seconds=0.1)
    assert TestClient(app).get("/health/ready").status_code == 200


def test_non_http_scopes_pass_through() -> None:
    """Lifespan and websocket scopes have no request to time out."""
    calls: list[str] = []

    async def inner(scope: object, receive: object, send: object) -> None:
        calls.append("called")

    mw = TimeoutMiddleware(inner, seconds=0.1)  # type: ignore[arg-type]

    async def run() -> None:
        async def receive() -> dict[str, str]:  # pragma: no cover
            return {}

        async def send(message: object) -> None:  # pragma: no cover
            return None

        await mw({"type": "lifespan"}, receive, send)  # type: ignore[arg-type]

    anyio.run(run)
    assert calls == ["called"]


def test_zero_or_negative_timeout_is_rejected() -> None:
    """A timeout of 0 would fail every request; silently accepting it would make
    the misconfiguration look like a service outage."""
    app = FastAPI()
    with pytest.raises(ValueError, match="seconds must be"):
        TimeoutMiddleware(app, seconds=0)


def test_timeout_response_carries_the_request_id(capture_logs: list[dict]) -> None:
    """Installed innermost, so the 504 travels back out through the request
    context like any other response: it gets the header, the body extension and
    an access-log line. A timeout with no request ID is the one response nobody
    can trace, and it is precisely the response someone comes asking about.

    (That it is also counted by the metrics layer, which sits further out
    still, is asserted in test_metrics.py where the meter fixture lives.)
    """
    app = FastAPI()

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        await anyio.sleep(5)
        return {}

    apply_standard_middleware(
        app, BaseAppSettings(_env_file=None, http=HttpSettings(timeout_seconds=0.1))
    )
    resp = TestClient(app).get("/slow")

    assert resp.status_code == 504
    assert resp.headers["X-Request-ID"]
    assert resp.json()["request_id"] == resp.headers["X-Request-ID"]

    access = [e for e in capture_logs if e.get("event") == "request_completed"]
    assert access and access[0]["status_code"] == 504
