"""Reject oversized request bodies before anything buffers them (pure ASGI)."""

from __future__ import annotations

from collections.abc import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from py_common.errors import ErrorCode, problem_type_uri
from py_common.http.middleware.request_context import client_ip
from py_common.http.problem import problem_response
from py_common.logging import current_request_id, get_logger

logger = get_logger(__name__)

DEFAULT_MAX_BODY_BYTES = 1024 * 1024  # 1 MiB


class BodyTooLarge(Exception):
    """Raised out of ``receive`` when the streamed body passes the limit.

    Internal to the middleware: it is raised inside the handler's call to read
    the body and caught one frame away, in ``__call__``. It exists as a real
    exception rather than a sentinel message because the handler must *stop*,
    and returning a short body would instead hand it a truncated payload to
    parse as if it were complete.
    """


class BodySizeLimitMiddleware:
    """Cap the request body a service will accept.

    A JSON endpoint with no limit will happily buffer whatever it is sent.
    ``json.loads`` on a gigabyte allocates several more, so one request from one
    client can take the process down — no volume needed, which is what separates
    this from rate limiting.

    Two checks, because either alone is insufficient:

    * ``Content-Length``, when present, is rejected before a single byte of body
      is read. That is the cheap path and covers ordinary clients.
    * The streamed bytes are counted regardless, because ``Content-Length`` is
      optional under chunked transfer encoding and is in any case a claim by the
      caller. A limit that trusts it stops honest clients and nobody else.

    Install innermost, which :func:`~py_common.http.middleware.apply_standard_middleware`
    does. Exceeding the limit mid-stream raises out of the handler's own
    ``await request.body()``, and that exception must reach this middleware
    before any layer that renders unhandled exceptions as 500s — otherwise a
    body that is too large is reported as a server error, and the client is told
    the fault was ours.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int = DEFAULT_MAX_BODY_BYTES,
        exclude_paths: Iterable[str] = (),
    ) -> None:
        """
        ``exclude_paths`` exempts endpoints that legitimately take large bodies —
        file upload, bulk import. Prefer exempting those few over raising the
        global limit, which would hand every other endpoint the same allowance.
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")
        self.app = app
        self.max_bytes = max_bytes
        self.exclude_paths = frozenset(exclude_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.exclude_paths:
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await self._reject(scope, receive, send, size=declared, streamed=False)
            return

        received = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooLarge
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, send_wrapper)
        except BodyTooLarge:
            if started:
                # The handler answered before finishing the body. Nothing left to
                # replace, and it already said its piece.
                return
            await self._reject(scope, receive, send, size=received, streamed=True)

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        size: int,
        streamed: bool,
    ) -> None:
        logger.warning(
            "request_body_too_large",
            http={"request": {"method": scope.get("method"), "bytes": size}},
            url={"path": scope.get("path")},
            max_bytes=self.max_bytes,
            client={"ip": client_ip(scope)},
            detected_while_streaming=streamed,
        )
        response = problem_response(
            title="Content Too Large",
            status_code=413,
            detail=f"Request body exceeds the {self.max_bytes} byte limit",
            instance=str(scope.get("path", "")),
            type_=problem_type_uri(ErrorCode.PAYLOAD_TOO_LARGE, base_url=_base_url(scope)),
            error_code=int(ErrorCode.PAYLOAD_TOO_LARGE),
            request_id=current_request_id(),
        )
        await response(scope, receive, send)


def _content_length(scope: Scope) -> int | None:
    for key, value in scope.get("headers", ()):
        if key == b"content-length":
            try:
                return int(value)
            except ValueError:
                # A malformed header is the server's problem to reject, not
                # something to guess at; fall through to counting bytes.
                return None
    return None


def _base_url(scope: Scope) -> str | None:
    state = getattr(scope.get("app"), "state", None)
    value = getattr(state, "problem_type_base_url", None)
    return value if isinstance(value, str) else None
