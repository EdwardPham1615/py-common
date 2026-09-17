"""gRPC request-id interceptor unit tests (no live server required)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import grpc
import pytest
import structlog

from py_common.runtime.grpc_interceptors import (
    REQUEST_ID_METADATA_KEY,
    RequestIdClientInterceptor,
    RequestIdServerInterceptor,
)


async def _intercept(handler: Any, metadata: tuple[tuple[str, str], ...] = ()) -> Any:
    """Run the server interceptor over ``handler`` and return what it installed."""

    async def continuation(details: object) -> Any:
        return handler

    details = SimpleNamespace(invocation_metadata=metadata, method="/probe.Echo/Call")
    return await RequestIdServerInterceptor().intercept_service(continuation, details)


def _bound_request_id() -> Any:
    return structlog.contextvars.get_contextvars().get("request_id")


async def test_server_interceptor_binds_incoming_request_id() -> None:
    """The ID is visible *inside* the handler — not merely after the lookup."""
    seen: list[Any] = []

    async def behavior(request: Any, context: Any) -> str:
        seen.append(_bound_request_id())
        return "response"

    handler = await _intercept(
        grpc.unary_unary_rpc_method_handler(behavior),
        ((REQUEST_ID_METADATA_KEY, "incoming-rid"),),
    )

    structlog.contextvars.clear_contextvars()
    assert await handler.unary_unary("req", None) == "response"
    assert seen == ["incoming-rid"]


async def test_server_interceptor_generates_when_missing() -> None:
    seen: list[Any] = []

    async def behavior(request: Any, context: Any) -> None:
        seen.append(_bound_request_id())

    handler = await _intercept(grpc.unary_unary_rpc_method_handler(behavior))

    structlog.contextvars.clear_contextvars()
    await handler.unary_unary("req", None)
    assert isinstance(seen[0], str) and seen[0]


async def test_server_interceptor_unbinds_after_the_handler() -> None:
    """The regression: the ID used to outlive its RPC forever.

    Anything inheriting that context afterwards — the next RPC, a background
    task, an outbound call reading ``current_request_id()`` — reported the
    stale ID and silently merged two unrelated requests in log search.
    """

    async def behavior(request: Any, context: Any) -> None:
        return None

    handler = await _intercept(
        grpc.unary_unary_rpc_method_handler(behavior),
        ((REQUEST_ID_METADATA_KEY, "transient-rid"),),
    )

    structlog.contextvars.clear_contextvars()
    await handler.unary_unary("req", None)
    assert "request_id" not in structlog.contextvars.get_contextvars()


async def test_server_interceptor_restores_the_surrounding_context() -> None:
    """Restore what was bound before, rather than clearing the whole context."""

    async def behavior(request: Any, context: Any) -> Any:
        return _bound_request_id()

    handler = await _intercept(
        grpc.unary_unary_rpc_method_handler(behavior),
        ((REQUEST_ID_METADATA_KEY, "inner-rid"),),
    )

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id="outer-rid", service="probe")
    try:
        assert await handler.unary_unary("req", None) == "inner-rid"
        assert structlog.contextvars.get_contextvars() == {
            "request_id": "outer-rid",
            "service": "probe",
        }
    finally:
        structlog.contextvars.clear_contextvars()


async def test_server_interceptor_unbinds_when_the_handler_raises() -> None:
    async def behavior(request: Any, context: Any) -> None:
        raise RuntimeError("handler blew up")

    handler = await _intercept(
        grpc.unary_unary_rpc_method_handler(behavior),
        ((REQUEST_ID_METADATA_KEY, "failing-rid"),),
    )

    structlog.contextvars.clear_contextvars()
    with pytest.raises(RuntimeError, match="handler blew up"):
        await handler.unary_unary("req", None)
    assert "request_id" not in structlog.contextvars.get_contextvars()


async def test_sequential_rpcs_each_see_their_own_request_id() -> None:
    seen: list[Any] = []

    async def behavior(request: Any, context: Any) -> None:
        seen.append(_bound_request_id())

    structlog.contextvars.clear_contextvars()
    for rid in ("rid-first", "rid-second"):
        handler = await _intercept(
            grpc.unary_unary_rpc_method_handler(behavior),
            ((REQUEST_ID_METADATA_KEY, rid),),
        )
        await handler.unary_unary("req", None)

    assert seen == ["rid-first", "rid-second"]


async def test_streaming_handler_keeps_the_id_bound_for_every_message() -> None:
    """A streaming handler is still running long after ``continuation`` returned."""
    seen: list[Any] = []

    async def behavior(request: Any, context: Any) -> AsyncIterator[str]:
        for index in range(3):
            seen.append(_bound_request_id())
            yield f"msg-{index}"

    handler = await _intercept(
        grpc.unary_stream_rpc_method_handler(behavior),
        ((REQUEST_ID_METADATA_KEY, "stream-rid"),),
    )

    structlog.contextvars.clear_contextvars()
    messages = [message async for message in handler.unary_stream("req", None)]
    assert messages == ["msg-0", "msg-1", "msg-2"]
    assert seen == ["stream-rid"] * 3
    assert "request_id" not in structlog.contextvars.get_contextvars()


async def test_streaming_handler_may_be_a_coroutine_returning_an_iterable() -> None:
    """gRPC accepts both shapes, so the wrapper has to unwrap the awaitable one."""
    seen: list[Any] = []

    async def inner() -> AsyncIterator[str]:
        seen.append(_bound_request_id())
        yield "only"

    async def behavior(request: Any, context: Any) -> AsyncIterator[str]:
        return inner()

    handler = await _intercept(
        grpc.unary_stream_rpc_method_handler(behavior),
        ((REQUEST_ID_METADATA_KEY, "awaitable-rid"),),
    )

    structlog.contextvars.clear_contextvars()
    assert [m async for m in handler.unary_stream("req", None)] == ["only"]
    assert seen == ["awaitable-rid"]
    assert "request_id" not in structlog.contextvars.get_contextvars()


async def test_server_interceptor_passes_through_an_unknown_method() -> None:
    """No handler means no behavior to wrap — and no request ID left behind."""
    structlog.contextvars.clear_contextvars()
    assert await _intercept(None) is None
    assert "request_id" not in structlog.contextvars.get_contextvars()


async def test_server_interceptor_leaves_a_behaviorless_handler_alone() -> None:
    handler = grpc.unary_unary_rpc_method_handler(None)
    assert await _intercept(handler) is handler


async def test_client_interceptor_attaches_request_id() -> None:
    interceptor = RequestIdClientInterceptor()
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id="outbound-rid")

    captured: dict[str, object] = {}

    async def continuation(details: object, request: object) -> str:
        captured["details"] = details
        return "ok"

    details = MagicMock()
    details.method = "/svc/Method"
    details.timeout = None
    details.metadata = None
    details.credentials = None
    details.wait_for_ready = None

    try:
        result = await interceptor.intercept_unary_unary(continuation, details, {})
        assert result == "ok"
        new_details = captured["details"]
        metadata = list(getattr(new_details, "metadata", []))
        assert (REQUEST_ID_METADATA_KEY, "outbound-rid") in metadata
    finally:
        structlog.contextvars.clear_contextvars()
