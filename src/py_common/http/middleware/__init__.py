"""Common reusable HTTP middleware."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from py_common.http.middleware.body_limit import (
    DEFAULT_MAX_BODY_BYTES,
    BodySizeLimitMiddleware,
    BodyTooLarge,
)
from py_common.http.middleware.idempotency import (
    IDEMPOTENCY_HEADER,
    REPLAY_HEADER,
    IdempotencyMiddleware,
)
from py_common.http.middleware.metrics import MetricsMiddleware
from py_common.http.middleware.request_context import (
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
    client_ip,
)
from py_common.http.middleware.security_headers import (
    API_CONTENT_SECURITY_POLICY,
    SecurityHeadersMiddleware,
)
from py_common.http.middleware.timeout import (
    DEFAULT_EXCLUDE_PATHS,
    TimeoutMiddleware,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from redis.asyncio import Redis

    from py_common.config import BaseAppSettings

__all__ = [
    "API_CONTENT_SECURITY_POLICY",
    "DEFAULT_EXCLUDE_PATHS",
    "DEFAULT_MAX_BODY_BYTES",
    "IDEMPOTENCY_HEADER",
    "REPLAY_HEADER",
    "REQUEST_ID_HEADER",
    "BodySizeLimitMiddleware",
    "BodyTooLarge",
    "IdempotencyMiddleware",
    "MetricsMiddleware",
    "RequestContextMiddleware",
    "SecurityHeadersMiddleware",
    "TimeoutMiddleware",
    "apply_standard_middleware",
    "client_ip",
]


def apply_standard_middleware(
    app: FastAPI,
    settings: BaseAppSettings,
    *,
    metrics: bool = True,
    redis: Redis | None = None,
) -> None:
    """Attach the standard middleware stack in the correct order.

    Outermost to innermost: CORS, security headers, metrics, request context
    (request-ID + access log + unhandled-exception rendering), gzip, timeout,
    body limit, idempotency. Starlette treats the *last* added middleware as
    outermost, hence the reversed add order below.

    Request context must sit *inside* the other two: it renders unhandled
    exceptions itself (see :class:`RequestContextMiddleware`), and that response
    only picks up security and CORS headers if those layers wrap it. Metrics sit
    just outside it so those rendered 500s are counted as 500s.

    ``metrics`` records RED metrics through the OTel API; they stay no-ops until
    :func:`~py_common.telemetry.metrics.setup_metrics` installs a provider, so
    leaving it on costs nothing in a service that exports no metrics.

    Everything that varies between deployments is read from settings —
    ``settings.http`` (``HTTP__TIMEOUT_SECONDS``, ``HTTP__CONTENT_SECURITY_POLICY``,
    ``HTTP__HSTS``, ``HTTP__HSTS_MAX_AGE``, ``HTTP__MAX_BODY_BYTES``,
    ``HTTP__GZIP_MIN_SIZE``) and ``settings.cors`` (``CORS__ORIGINS`` and
    friends) — so a value has exactly one source and an operator can change it
    without a code change. ``metrics`` stays an argument because it is
    structural rather than environment-specific: whether this service records
    HTTP metrics at all, not what its ceiling should be.

    ``redis`` installs :class:`IdempotencyMiddleware`. It is an argument rather
    than a setting because it is a live connection, not a value; its TTL comes
    from ``HTTP__IDEMPOTENCY_TTL_SECONDS``.
    """
    # Innermost of all. It buffers the request body to fingerprint it, so the
    # size check has to have run already -- installing it outside the body limit
    # would let an unbounded body be buffered here before anything measured it.
    if redis is not None:
        app.add_middleware(
            IdempotencyMiddleware,
            redis=redis,
            ttl_seconds=settings.http.idempotency_ttl_seconds,
        )

    # Then the body limit: BodyTooLarge is raised out of the handler's own read of
    # the body, and it has to be caught here rather than by any layer that turns
    # unhandled exceptions into 500s -- otherwise an oversized body is reported
    # as a server error and the client is told the fault was ours.
    if settings.http.max_body_bytes is not None:
        app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.http.max_body_bytes)

    # Then the timeout, so the 504 it produces travels back out through the
    # request context (request ID, access log) and the metrics layer (counted as
    # a 504) exactly like any other response.
    if settings.http.timeout_seconds is not None:
        app.add_middleware(TimeoutMiddleware, seconds=settings.http.timeout_seconds)

    # Compression sits outside the timeout -- the deadline is a ceiling on the
    # handler, not on serialising what it returned -- and outside idempotency,
    # which is the boundary that matters: that middleware stores a response and
    # replays it for a repeated key, so a compressed body in the store would be
    # replayed verbatim to a client that never sent Accept-Encoding: gzip.
    # Store the plain body, compress per request.
    #
    # It stays *inside* metrics and the request context so the time compression
    # costs lands in http.server.request.duration and the access log, rather
    # than hiding outside the numbers you alert on.
    #
    # Starlette's implementation, not ours: it already excludes
    # text/event-stream and pre-compressed media types, sets Vary, and moves
    # payloads over 128 KiB onto a worker thread so a large response cannot
    # stall the event loop.
    if settings.http.gzip_min_size is not None:
        app.add_middleware(GZipMiddleware, minimum_size=settings.http.gzip_min_size)

    app.add_middleware(RequestContextMiddleware)
    if metrics:
        app.add_middleware(MetricsMiddleware)
    app.add_middleware(
        SecurityHeadersMiddleware,
        content_security_policy=settings.http.content_security_policy,
        hsts=settings.http.hsts,
        hsts_max_age=settings.http.hsts_max_age,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors.origins,
        allow_credentials=settings.cors.allow_credentials,
        allow_methods=settings.cors.allow_methods,
        allow_headers=settings.cors.allow_headers,
    )
