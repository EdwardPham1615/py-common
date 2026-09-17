"""RED metrics for inbound HTTP requests (pure ASGI)."""

from __future__ import annotations

import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from py_common.telemetry.metrics import (
    http_server_active_requests,
    http_server_duration,
)

# Methods worth keeping as their own time series. Anything else becomes
# ``_OTHER``: the method comes straight off the wire, so without this a scanner
# probing random verbs mints an unbounded number of series in the backend.
KNOWN_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "DELETE", "CONNECT", "OPTIONS", "TRACE", "PATCH"}
)

# Probes and the scrape endpoint itself, which would otherwise dominate the
# request rate and drag the latency histogram toward zero.
DEFAULT_EXCLUDED_PATHS = ("/health", "/live", "/ready", "/metrics")


def _normalize_method(method: str) -> str:
    return method if method in KNOWN_METHODS else "_OTHER"


def _route_template(scope: Scope) -> str | None:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else None


class MetricsMiddleware:
    """Record ``http.server.request.duration`` and ``http.server.active_requests``.

    Kept separate from :class:`~py_common.http.middleware.request_context.RequestContextMiddleware`
    even though both time the request: metrics and access logs have different
    blast radii — a bad attribute here costs money in the metrics backend, a bad
    field there only costs log volume — and services that export metrics some
    other way can drop this layer without losing their access log.

    **Cardinality is the whole design.** Every attribute is drawn from a bounded
    set: the normalized method, the response status, and ``http.route`` — the
    *template* (``/users/{id}``), never the raw path. Unmatched requests (404,
    or anything short-circuited before routing) carry no route attribute at all
    rather than their path, because that path is attacker-controlled.

    Sits *outside* ``RequestContextMiddleware`` so that unhandled exceptions,
    which that layer turns into a 500 Problem Details response, are counted as
    ``http.response.status_code=500`` like any other error response.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        excluded_paths: tuple[str, ...] | frozenset[str] | None = None,
    ) -> None:
        self.app = app
        self.excluded_paths = tuple(
            DEFAULT_EXCLUDED_PATHS if excluded_paths is None else excluded_paths
        )

    def _excluded(self, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in self.excluded_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._excluded(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        method = _normalize_method(scope.get("method", ""))
        active_attrs = {"http.request.method": method}
        http_server_active_requests.add(1, active_attrs)

        status_code = 500
        error_type: str | None = None
        start = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            error_type = type(exc).__qualname__
            raise
        finally:
            duration = time.perf_counter() - start
            http_server_active_requests.add(-1, active_attrs)

            attrs: dict[str, Any] = {
                "http.request.method": method,
                "http.response.status_code": status_code,
            }
            route = _route_template(scope)
            if route:
                attrs["http.route"] = route
            if error_type is not None:
                attrs["error.type"] = error_type
            http_server_duration.record(duration, attrs)
