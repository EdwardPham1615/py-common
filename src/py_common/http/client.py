"""Standardized httpx AsyncClient factory for service-to-service calls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from py_common.logging import current_request_id

if TYPE_CHECKING:
    from py_common.utils.circuit_breaker import AsyncCircuitBreaker

REQUEST_ID_HEADER = "X-Request-ID"


class CircuitBreakerTransport(httpx.AsyncBaseTransport):
    """Gate every request on an :class:`AsyncCircuitBreaker`.

    A transport rather than httpx event hooks: response hooks only fire when a
    response exists, so a hook-based breaker never sees connection errors or
    timeouts. It would count 5xx only — staying closed in exactly the case a
    breaker exists for, an upstream that is fully down.

    Wrapping the retrying transport (rather than being wrapped by it) means one
    logical request counts as one outcome, however many connect retries it took.
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport,
        breaker: AsyncCircuitBreaker,
    ) -> None:
        self._transport = transport
        self._breaker = breaker

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._breaker.before_call()
        try:
            response = await self._transport.handle_async_request(request)
        except Exception:
            self._breaker.on_failure()
            raise
        if response.status_code >= 500:
            self._breaker.on_failure()
        else:
            self._breaker.on_success()
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


async def _propagate_request_id(request: httpx.Request) -> None:
    """Forward the current request ID (bound by RequestContextMiddleware) downstream."""
    if REQUEST_ID_HEADER in request.headers:
        return
    request_id = current_request_id()
    if request_id:
        request.headers[REQUEST_ID_HEADER] = request_id


def create_http_client(
    *,
    base_url: str = "",
    timeout: float = 10.0,
    connect_retries: int = 3,
    propagate_request_id: bool = True,
    circuit_breaker: AsyncCircuitBreaker | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """Create an ``httpx.AsyncClient`` with standard timeout, connect retries,
    and request-ID propagation.

    When ``circuit_breaker`` is provided, every request is gated by it — open
    circuits raise :class:`~py_common.utils.CircuitOpenError` before the request
    is sent (useful for partner/payment gateways). Both transport errors
    (connect refused, timeouts) and 5xx responses count as failures.

    Trace-context propagation is handled by OTel's httpx instrumentation (see
    ``py_common.telemetry``). Keep one client per upstream service alive for the
    app's lifetime (e.g. via a ``LifespanResource``) instead of creating one
    per request.

    ``transport`` replaces the default retrying transport — useful for tests
    (``httpx.MockTransport``) or a custom transport. The circuit breaker, when
    given, wraps whichever transport is in use.
    """
    resolved: httpx.AsyncBaseTransport = transport or httpx.AsyncHTTPTransport(
        retries=connect_retries
    )
    if circuit_breaker is not None:
        resolved = CircuitBreakerTransport(resolved, circuit_breaker)

    event_hooks: dict[str, list[Any]] = kwargs.pop("event_hooks", {"request": [], "response": []})
    if propagate_request_id:
        event_hooks.setdefault("request", []).append(_propagate_request_id)

    return httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        transport=resolved,
        event_hooks=event_hooks,
        **kwargs,
    )
