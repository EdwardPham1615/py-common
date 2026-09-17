"""Request-ID + OpenTelemetry context binding for structlog (pure ASGI)."""

from __future__ import annotations

import time
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlencode

import structlog
from opentelemetry import trace
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from py_common.http.problem import (
    APP_STATE_PROBLEM_TYPE_BASE_URL,
    unhandled_problem_response,
)

REQUEST_ID_HEADER = "X-Request-ID"

# Query keys whose values are replaced with "***" in access logs.
DEFAULT_MASK_QUERY_PARAMS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "api_key",
        "apikey",
        "authorization",
    }
)


def client_ip(scope: Scope) -> str | None:
    """Return the client IP as resolved by the ASGI server.

    Reads ``scope["client"]`` and nothing else. ``X-Forwarded-For`` is
    deliberately *not* parsed here: any client can send that header, so trusting
    it unconditionally lets a caller forge its own address in access logs and
    rate-limit buckets.

    Resolving it is the ASGI server's job, because only the server knows which
    peer is a trusted proxy. Configure uvicorn's ``forwarded_allow_ips``
    (``FORWARDED_ALLOW_IPS``) and ``scope["client"]`` becomes the real client —
    it defaults to ``127.0.0.1``, which never matches an ingress pod, so
    behind Kubernetes it stays the ingress address until you set it. See
    "Deploying behind a proxy" in the README.

    Shared by the access log and the rate-limit dependency so both agree on who
    the caller is.
    """
    client = scope.get("client")
    if isinstance(client, (list, tuple)) and client:
        return str(client[0])
    return None


def _mask_query(raw_query: bytes | str, mask_params: frozenset[str]) -> str | None:
    if not raw_query:
        return None
    query = raw_query.decode() if isinstance(raw_query, bytes) else raw_query
    if not query:
        return None
    pairs = parse_qsl(query, keep_blank_values=True)
    masked = [(k, "***" if k.lower() in mask_params else v) for k, v in pairs]
    return urlencode(masked)


def _route_template(scope: Scope) -> str | None:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else None


def _problem_type_base_url(scope: Scope) -> str | None:
    state = getattr(scope.get("app"), "state", None)
    value = getattr(state, APP_STATE_PROBLEM_TYPE_BASE_URL, None)
    return value if isinstance(value, str) else None


def _user_id(scope: Scope) -> str | None:
    state = scope.get("state")
    if not isinstance(state, dict):
        # Starlette State object
        user = getattr(state, "user", None) if state is not None else None
    else:
        user = state.get("user")
    if user is None:
        return None
    sub = getattr(user, "sub", None)
    return str(sub) if sub is not None else None


class RequestContextMiddleware:
    """Generate/propagate request ID, bind OTel + request context to structlog,
    and emit one structured access-log line per request.

    Pure-ASGI implementation (no ``BaseHTTPMiddleware``) so it does not break
    streaming responses or spawn an extra task per request.

    Access-log fields (ECS-inspired): ``request_id``, ``status_code``,
    ``duration_ms``, ``http.request.method``, ``url.path``, ``http.route``,
    ``url.query`` (maskable), ``client.address``, ``user_agent.original``,
    ``user.id`` (when ``request.state.user`` is set by auth), plus trace/span IDs.

    Also renders unhandled exceptions as Problem Details (``handle_exceptions``,
    on by default). Starlette runs the ``Exception`` handler in
    ``ServerErrorMiddleware`` — *outside* every user middleware — so a 500 built
    there never passes back through CORS, this middleware, or the security
    headers. Catching here instead means error responses carry the same
    ``X-Request-ID``, CORS and security headers as successful ones, are logged
    with exactly one traceback, and still appear in the access log as
    ``status_code=500``.

    Because the exception is fully handled, it does not propagate. In tests use
    ``TestClient(app, raise_server_exceptions=False)`` and assert on the 500
    response, or pass ``handle_exceptions=False`` to let it bubble up.
    """

    def __init__(
        self,
        app: ASGIApp,
        header_name: str = REQUEST_ID_HEADER,
        *,
        mask_query_params: frozenset[str] | set[str] | None = None,
        handle_exceptions: bool = True,
    ) -> None:
        self.app = app
        self.header_name = header_name
        self.mask_query_params = frozenset(mask_query_params or DEFAULT_MASK_QUERY_PARAMS)
        self.handle_exceptions = handle_exceptions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = headers.get(self.header_name) or str(uuid.uuid4())
        path = scope.get("path", "")
        method = scope.get("method", "")
        query = _mask_query(scope.get("query_string", b""), self.mask_query_params)
        client_addr = client_ip(scope)
        user_agent = headers.get("user-agent")

        structlog.contextvars.clear_contextvars()
        bind: dict[str, Any] = {
            "request_id": request_id,
            "http": {"request": {"method": method, "id": request_id}},
            "url": {"path": path},
        }
        if query is not None:
            bind["url"]["query"] = query
        if client_addr:
            bind["client"] = {"address": client_addr}
        if user_agent:
            bind["user_agent"] = {"original": user_agent}

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx.is_valid:
            bind["trace"] = {"id": format(ctx.trace_id, "032x")}
            bind["span"] = {"id": format(ctx.span_id, "016x")}
            span.set_attribute("http.request.id", request_id)
        structlog.contextvars.bind_contextvars(**bind)

        scope.setdefault("state", {})["request_id"] = request_id

        status_code = 500
        response_started = False
        start = time.perf_counter()
        logger = structlog.get_logger("access")

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                response_headers = MutableHeaders(scope=message)
                response_headers[self.header_name] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if response_started or not self.handle_exceptions:
                # Headers are already on the wire (or the caller opted out), so
                # there is no valid response left to send — let it propagate.
                duration_ms = (time.perf_counter() - start) * 1000
                logger.exception("request_failed", duration_ms=round(duration_ms, 2))
                raise
            logger.exception("unhandled_exception", method=method, path=path)
            response = unhandled_problem_response(
                path=path,
                request_id=request_id,
                base_url=_problem_type_base_url(scope),
            )
            await response(scope, receive, send_wrapper)

        duration_ms = (time.perf_counter() - start) * 1000
        route = _route_template(scope)
        user_id = _user_id(scope)
        log_extra: dict[str, Any] = {
            "status_code": status_code,
            "duration_ms": round(duration_ms, 2),
        }
        if route:
            log_extra["http"] = {
                **bind.get("http", {}),
                "route": route,
            }
        if user_id:
            log_extra["user"] = {"id": user_id}
        logger.info("request_completed", **log_extra)
