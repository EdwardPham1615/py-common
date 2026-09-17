"""Per-request wall-clock timeout (pure ASGI)."""

from __future__ import annotations

import math
import time
from collections.abc import Iterable

import anyio
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from py_common.errors import ErrorCode, problem_type_uri
from py_common.http.middleware.request_context import client_ip
from py_common.http.problem import problem_response
from py_common.logging import current_request_id, get_logger

logger = get_logger(__name__)

DEFAULT_EXCLUDE_PATHS = frozenset({"/health", "/health/live", "/health/ready", "/metrics"})


class TimeoutMiddleware:
    """Cap how long a handler may take before the request is abandoned.

    Without one, a handler blocked on an upstream that never answers holds its
    connection, its database session and its worker slot indefinitely. Enough of
    them and the service stops serving anything, while every health check still
    passes — the process is fine, it is just entirely occupied.

    **The timeout covers time-to-first-byte, not the whole response.** Once the
    handler has produced ``http.response.start`` the deadline is lifted and the
    body streams for as long as it needs. A wall-clock limit on the complete response
    would kill exactly the endpoints that legitimately run long — server-sent
    events, large downloads, streamed exports — and those are not the failure
    this exists to catch.

    On expiry the handler task is **cancelled**, not merely abandoned. Answering
    504 while the work continues would leave the database session checked out and
    the upstream call in flight, which is the resource leak this is meant to
    prevent, minus the visibility.

    If the response has already started when the timeout fires there is no way to
    send a 504 — the status line is gone. The connection is closed instead, and
    the event is logged, because a truncated response is worth knowing about.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        seconds: float = 30.0,
        exclude_paths: Iterable[str] = DEFAULT_EXCLUDE_PATHS,
    ) -> None:
        """
        ``seconds`` should sit **below** whatever the ingress in front of the
        service uses, so the timeout that fires is the one that can explain
        itself: this one returns Problem Details with a request ID that appears
        in the logs, while a proxy timeout returns the proxy's own error page and
        leaves the handler running.

        ``exclude_paths`` defaults to the probe and metrics endpoints. Probes
        carry their own timeouts, and a readiness check racing a middleware
        timeout produces two different answers to one question.
        """
        if seconds <= 0:
            raise ValueError("seconds must be > 0")
        self.app = app
        self.seconds = seconds
        self.exclude_paths = frozenset(exclude_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.exclude_paths:
            await self.app(scope, receive, send)
            return

        started = False
        began_at = time.perf_counter()

        # move_on_after rather than fail_after, because the deadline has to be
        # revocable: once the handler produces a status line the clock stops and
        # the body streams freely. fail_after would keep counting and cancel a
        # perfectly healthy download partway through.
        with anyio.move_on_after(self.seconds) as cancel_scope:

            async def send_wrapper(message: Message) -> None:
                nonlocal started
                if message["type"] == "http.response.start":
                    started = True
                    cancel_scope.deadline = math.inf
                await send(message)

            await self.app(scope, receive, send_wrapper)

        if cancel_scope.cancelled_caught:
            elapsed_ms = round((time.perf_counter() - began_at) * 1000, 2)
            logger.warning(
                "request_timeout",
                http={"request": {"method": scope.get("method")}},
                url={"path": scope.get("path")},
                duration_ms=elapsed_ms,
                timeout_seconds=self.seconds,
                client={"ip": client_ip(scope)},
                response_started=started,
            )
            if started:
                # The status line is already on the wire; there is no way to
                # replace it with a 504. Returning here ends the response, which
                # the server turns into a closed/truncated connection.
                return
            await self._send_timeout(scope, receive, send)

    async def _send_timeout(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = problem_response(
            title="Gateway Timeout",
            status_code=504,
            detail=f"The request did not complete within {self.seconds:g} seconds",
            instance=str(scope.get("path", "")),
            type_=problem_type_uri(ErrorCode.TIMEOUT, base_url=_base_url(scope)),
            error_code=int(ErrorCode.TIMEOUT),
            # Bound by RequestContextMiddleware, which wraps this one. Without
            # it a 504 is the one response nobody can trace back to its logs --
            # and a timeout is precisely the response someone will come asking
            # about.
            request_id=current_request_id(),
        )
        await response(scope, receive, send)


def _base_url(scope: Scope) -> str | None:
    state = getattr(scope.get("app"), "state", None)
    value = getattr(state, "problem_type_base_url", None)
    return value if isinstance(value, str) else None
