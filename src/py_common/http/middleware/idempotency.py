"""Idempotency-Key handling for unsafe requests (pure ASGI, Redis-backed)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from py_common.errors import ErrorCode, problem_type_uri
from py_common.http.problem import problem_response
from py_common.logging import current_request_id, get_logger

logger = get_logger(__name__)

IDEMPOTENCY_HEADER = "idempotency-key"
REPLAY_HEADER = "Idempotent-Replay"
DEFAULT_METHODS = frozenset({"POST", "PATCH", "DELETE"})
DEFAULT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_MAX_STORED_BYTES = 256 * 1024

_IN_FLIGHT = "in-flight"


class IdempotencyMiddleware:
    """Replay the first response when a client repeats a request with the same key.

    A client whose connection drops after the server committed, but before the
    response arrived, has no way to tell "the order was created" from "the order
    was not created". Its only safe options are to give up or to retry, and
    retrying without this creates a second order. The key is what lets it retry.

    Scope of a key: ``caller + method + path + key``. The caller is part of it
    because a key is chosen by the client, and two clients will eventually
    choose the same one — without scoping, the second would be handed the
    first's response, which is a data leak rather than a collision. Method and
    path are included because the same key on a different endpoint is a
    different operation.

    The body is fingerprinted. Reusing a key with different content is a client
    bug, and answering it with the first response would silently discard the
    second request; it gets 409 instead.

    5xx responses are **not** stored. An idempotency record for a server error
    would make the failure permanent for that key: every retry would replay the
    500 rather than getting the second chance the client is asking for.
    """

    def __init__(
        self,
        app: ASGIApp,
        redis: Redis,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        methods: Iterable[str] = DEFAULT_METHODS,
        namespace: str = "idem",
        max_stored_bytes: int = DEFAULT_MAX_STORED_BYTES,
        fail_open: bool = False,
    ) -> None:
        """
        ``fail_open`` defaults to **False**, unlike the rate limiter and cache.

        Those degrade a convenience when Redis is unreachable; this one degrades
        the guarantee it exists to provide, and the damage is a duplicate
        payment rather than a slow page. Serving 503 is also safe for the client
        in a way it is not elsewhere: it is holding an idempotency key, so it
        can retry the moment Redis returns. Set it True only where a duplicate
        is cheaper than a rejection.
        """
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self.app = app
        self.redis = redis
        self.ttl_seconds = ttl_seconds
        self.methods = frozenset(m.upper() for m in methods)
        self.namespace = namespace
        self.max_stored_bytes = max_stored_bytes
        self.fail_open = fail_open

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "").upper() not in self.methods:
            await self.app(scope, receive, send)
            return

        key = _header(scope, IDEMPOTENCY_HEADER)
        if not key:
            # Not required: making it mandatory would break every existing
            # client the day it is switched on. A service that needs it enforced
            # can check for the header in a dependency.
            await self.app(scope, receive, send)
            return

        body, replay_receive = await _buffer_body(receive)
        redis_key = self._redis_key(scope, key)
        fingerprint = _fingerprint(scope, body)

        try:
            stored = await self._claim(redis_key, fingerprint)
        except RedisError:
            logger.warning(
                "idempotency_backend_unavailable", key=redis_key, fail_open=self.fail_open
            )
            if not self.fail_open:
                await self._conflict(
                    scope,
                    replay_receive,
                    send,
                    status_code=503,
                    detail="Idempotency store unavailable; retry with the same key",
                )
                return
            await self.app(scope, replay_receive, send)
            return

        if stored is not None:
            await self._handle_existing(scope, replay_receive, send, stored, fingerprint)
            return

        await self._run_and_record(scope, replay_receive, send, redis_key, fingerprint)

    # -- redis -------------------------------------------------------------

    def _redis_key(self, scope: Scope, key: str) -> str:
        caller = _caller(scope)
        raw = f"{caller}|{scope.get('method')}|{scope.get('path')}|{key}"
        digest = hashlib.sha256(raw.encode()).hexdigest()
        # Hash tag so a namespace stays on one Redis Cluster slot.
        return f"idempotency:{{{self.namespace}}}:{digest}"

    async def _claim(self, redis_key: str, fingerprint: str) -> dict[str, Any] | None:
        """Reserve the key, or return the record that already holds it.

        SET NX is what makes two simultaneous requests resolvable: exactly one
        wins the reservation and runs, and the loser finds a record rather than
        starting a second copy of the same operation.
        """
        marker = json.dumps({"state": _IN_FLIGHT, "fingerprint": fingerprint})
        won = await self.redis.set(redis_key, marker, nx=True, ex=self.ttl_seconds)
        if won:
            return None
        raw = await self.redis.get(redis_key)
        if raw is None:
            # Expired between the SET and the GET; treat as a fresh request
            # rather than failing on a race the client cannot see.
            return None
        parsed: dict[str, Any] = json.loads(raw)
        return parsed

    # -- outcomes ----------------------------------------------------------

    async def _handle_existing(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        stored: dict[str, Any],
        fingerprint: str,
    ) -> None:
        if stored.get("fingerprint") != fingerprint:
            await self._conflict(
                scope,
                receive,
                send,
                status_code=409,
                detail="This Idempotency-Key was already used with a different request body",
            )
            return

        if stored.get("state") == _IN_FLIGHT:
            await self._conflict(
                scope,
                receive,
                send,
                status_code=409,
                detail="A request with this Idempotency-Key is still in progress",
            )
            return

        await _replay(send, stored)

    async def _run_and_record(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        redis_key: str,
        fingerprint: str,
    ) -> None:
        status = 0
        headers: list[tuple[str, str]] = []
        chunks: list[bytes] = []
        too_large = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status, headers, too_large
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = [(k.decode(), v.decode()) for k, v in message.get("headers", [])]
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if sum(len(c) for c in chunks) + len(chunk) > self.max_stored_bytes:
                    too_large = True
                else:
                    chunks.append(chunk)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # The reservation must go, or a retry of a request that crashed
            # would replay nothing and be refused as in-flight until the TTL.
            await self._release(redis_key)
            raise

        if status >= 500 or too_large:
            # Not recorded: a stored 500 makes the failure permanent for this
            # key, and a truncated body would replay as a valid short response.
            await self._release(redis_key)
            return

        record = {
            "state": "done",
            "fingerprint": fingerprint,
            "status": status,
            "headers": headers,
            "body": b"".join(chunks).decode("latin-1"),
        }
        try:
            await self.redis.set(redis_key, json.dumps(record), ex=self.ttl_seconds)
        except RedisError:
            logger.warning("idempotency_record_failed", key=redis_key)

    async def _release(self, redis_key: str) -> None:
        try:
            await self.redis.delete(redis_key)
        except RedisError:
            logger.warning("idempotency_release_failed", key=redis_key)

    async def _conflict(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        detail: str,
    ) -> None:
        code = ErrorCode.IDEMPOTENCY if status_code == 409 else ErrorCode.SERVER
        response = problem_response(
            title="Idempotency Conflict" if status_code == 409 else "Service Unavailable",
            status_code=status_code,
            detail=detail,
            instance=str(scope.get("path", "")),
            type_=problem_type_uri(code, base_url=_base_url(scope)),
            error_code=int(code),
            request_id=current_request_id(),
        )
        await response(scope, receive, send)


# -- helpers ---------------------------------------------------------------


async def _buffer_body(receive: Receive) -> tuple[bytes, Receive]:
    """Read the whole body, and return a ``receive`` that can hand it over again.

    Unavoidable: the body has to be fingerprinted before the handler sees it,
    and an ASGI body can only be consumed once. The size is bounded by
    BodySizeLimitMiddleware, which is why that one wraps this.
    """
    chunks: list[bytes] = []
    more = True
    while more:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        more = bool(message.get("more_body", False))
    body = b"".join(chunks)

    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return body, replay


async def _replay(send: Send, stored: dict[str, Any]) -> None:
    headers = [
        (k.encode(), v.encode())
        for k, v in stored.get("headers", [])
        # Recomputed by the server for this response; replaying the old values
        # would describe a body that is no longer being sent.
        if k.lower() not in {"content-length", "date", "server"}
    ]
    body = stored.get("body", "").encode("latin-1")
    headers.append((b"content-length", str(len(body)).encode()))
    headers.append((REPLAY_HEADER.encode(), b"true"))
    await send({"type": "http.response.start", "status": stored["status"], "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _fingerprint(scope: Scope, body: bytes) -> str:
    raw = b"|".join(
        [
            str(scope.get("method", "")).encode(),
            str(scope.get("path", "")).encode(),
            scope.get("query_string", b""),
            body,
        ]
    )
    return hashlib.sha256(raw).hexdigest()


def _caller(scope: Scope) -> str:
    """Identify the caller so one client's key cannot collide with another's.

    Prefers the authenticated subject. Falls back to the peer address, which is
    weaker but still better than a global namespace where any client could be
    handed a stranger's stored response.
    """
    state = scope.get("state")
    user = state.get("user") if isinstance(state, dict) else None
    sub = getattr(user, "sub", None)
    if sub:
        return f"sub:{sub}"
    client = scope.get("client")
    return f"ip:{client[0]}" if client else "anonymous"


def _header(scope: Scope, name: str) -> str | None:
    target = name.encode()
    for key, value in scope.get("headers", ()):
        if key.lower() == target:
            decoded: str = value.decode()
            return decoded
    return None


def _base_url(scope: Scope) -> str | None:
    state = getattr(scope.get("app"), "state", None)
    value = getattr(state, "problem_type_base_url", None)
    return value if isinstance(value, str) else None
