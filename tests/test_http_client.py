"""Outbound httpx client factory: request-ID propagation and circuit breaking."""

from __future__ import annotations

import httpx
import pytest
import structlog

from py_common.http.client import REQUEST_ID_HEADER, create_http_client
from py_common.utils import AsyncCircuitBreaker, CircuitOpenError


class _ScriptedTransport(httpx.AsyncBaseTransport):
    """Replays a fixed list of outcomes: an int status, or an exception to raise."""

    def __init__(self, *outcomes: int | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, request=request)


def _client_with(transport: httpx.AsyncBaseTransport, **kwargs: object) -> httpx.AsyncClient:
    return create_http_client(base_url="http://upstream", transport=transport, **kwargs)  # type: ignore[arg-type]


async def test_propagates_request_id_from_contextvars() -> None:
    transport = _ScriptedTransport(200)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id="rid-outbound")

    async with _client_with(transport) as client:
        resp = await client.get("/downstream")

    assert resp.status_code == 200
    assert resp.request.headers[REQUEST_ID_HEADER] == "rid-outbound"
    structlog.contextvars.clear_contextvars()


async def test_explicit_request_id_header_wins() -> None:
    transport = _ScriptedTransport(200)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id="rid-context")

    async with _client_with(transport) as client:
        resp = await client.get("/downstream", headers={REQUEST_ID_HEADER: "rid-explicit"})

    assert resp.request.headers[REQUEST_ID_HEADER] == "rid-explicit"
    structlog.contextvars.clear_contextvars()


async def test_breaker_opens_on_transport_errors() -> None:
    """Connection failures must trip the breaker.

    Response event hooks never fire without a response, so a hook-based breaker
    counted 5xx only and stayed closed exactly when the upstream was fully down.
    """
    breaker = AsyncCircuitBreaker(failure_threshold=2, recovery_timeout=60, name="t")
    transport = _ScriptedTransport(httpx.ConnectError("refused"))

    async with _client_with(transport, circuit_breaker=breaker) as client:
        for _ in range(2):
            with pytest.raises(httpx.ConnectError):
                await client.get("/x")

        with pytest.raises(CircuitOpenError):
            await client.get("/x")

    # The short-circuited call never reached the network.
    assert transport.calls == 2


async def test_breaker_opens_on_server_errors() -> None:
    breaker = AsyncCircuitBreaker(failure_threshold=2, recovery_timeout=60, name="t")
    transport = _ScriptedTransport(503)

    async with _client_with(transport, circuit_breaker=breaker) as client:
        assert (await client.get("/x")).status_code == 503
        assert (await client.get("/x")).status_code == 503
        with pytest.raises(CircuitOpenError):
            await client.get("/x")


async def test_breaker_stays_closed_on_client_errors() -> None:
    """4xx is the caller's fault, not an upstream outage — it must not trip the breaker."""
    breaker = AsyncCircuitBreaker(failure_threshold=2, recovery_timeout=60, name="t")
    transport = _ScriptedTransport(404)

    async with _client_with(transport, circuit_breaker=breaker) as client:
        for _ in range(5):
            assert (await client.get("/x")).status_code == 404

    assert breaker.state.value == "closed"


async def test_no_breaker_leaves_requests_ungated() -> None:
    transport = _ScriptedTransport(httpx.ConnectError("refused"))
    async with _client_with(transport) as client:
        for _ in range(3):
            with pytest.raises(httpx.ConnectError):
                await client.get("/x")
    assert transport.calls == 3
