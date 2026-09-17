"""gRPC interceptors that propagate X-Request-ID across service boundaries."""

from __future__ import annotations

import inspect
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import grpc
import structlog
from grpc import aio

from py_common.logging import current_request_id
from py_common.runtime._grpc_behavior import behavior_field

REQUEST_ID_METADATA_KEY = "x-request-id"


def _metadata_value(metadata: Any, key: str) -> str | None:
    if metadata is None:
        return None
    for k, v in metadata:
        if k.lower() == key:
            return v if isinstance(v, str) else str(v)
    return None


class RequestIdServerInterceptor(aio.ServerInterceptor):  # type: ignore[misc]
    """Read ``x-request-id`` from inbound metadata (or generate one) and bind it
    to structlog contextvars so all logs / outbound calls share the same ID.

    The binding is scoped to the handler: established when the handler starts,
    held across every message a streaming handler yields, and unwound when it
    ends -- including when it raises. Binding in ``intercept_service`` itself
    was both too early and forever. Too early because ``continuation`` only
    *looks the handler up* and returns before it runs, which is the same reason
    :class:`~py_common.runtime.grpc_metrics.MetricsServerInterceptor` wraps the
    behavior. Forever because nothing unbound it, so the ID outlived its call
    and any later work inheriting that context -- a subsequent RPC, a
    background task, an outbound call reading
    :func:`~py_common.logging.current_request_id` -- reported a stale ID, which
    silently merges two unrelated requests in log search.

    ``bound_contextvars`` restores the previous context rather than clearing
    it, so a caller that bound its own keys around the RPC keeps them.
    """

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[Any]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> Any:
        request_id = _metadata_value(
            handler_call_details.invocation_metadata, REQUEST_ID_METADATA_KEY
        ) or str(uuid.uuid4())

        handler = await continuation(handler_call_details)
        if handler is None:
            return None

        field = behavior_field(handler)
        if field is None:
            return handler
        behavior = getattr(handler, field)

        if handler.response_streaming:

            async def stream_behavior(request: Any, context: Any) -> AsyncIterator[Any]:
                with structlog.contextvars.bound_contextvars(request_id=request_id):
                    result = behavior(request, context)
                    # A response-streaming handler may be an async generator
                    # function or a coroutine that returns an async iterable;
                    # gRPC accepts both, so both have to be unwrapped here.
                    if inspect.isawaitable(result):
                        result = await result
                    async for message in result:
                        yield message

            return handler._replace(**{field: stream_behavior})

        async def unary_behavior(request: Any, context: Any) -> Any:
            with structlog.contextvars.bound_contextvars(request_id=request_id):
                return await behavior(request, context)

        return handler._replace(**{field: unary_behavior})


def _with_request_id(client_call_details: aio.ClientCallDetails) -> aio.ClientCallDetails:
    """Return call details carrying the current request ID, if there is one."""
    request_id = current_request_id()
    if not request_id:
        return client_call_details

    metadata = list(client_call_details.metadata or [])
    if any(k.lower() == REQUEST_ID_METADATA_KEY for k, _ in metadata):
        return client_call_details
    metadata.append((REQUEST_ID_METADATA_KEY, str(request_id)))

    return aio.ClientCallDetails(
        client_call_details.method,
        client_call_details.timeout,
        metadata,
        client_call_details.credentials,
        client_call_details.wait_for_ready,
    )


class RequestIdClientInterceptor(aio.UnaryUnaryClientInterceptor):  # type: ignore[misc]
    """Attach the current request ID (from structlog contextvars) to outbound
    gRPC metadata so downstream services can correlate.
    """

    async def intercept_unary_unary(
        self,
        continuation: Callable[[aio.ClientCallDetails, Any], Awaitable[Any]],
        client_call_details: aio.ClientCallDetails,
        request: Any,
    ) -> Any:
        return await continuation(_with_request_id(client_call_details), request)


class RequestIdUnaryStreamClientInterceptor(aio.UnaryStreamClientInterceptor):  # type: ignore[misc]
    async def intercept_unary_stream(
        self,
        continuation: Callable[[aio.ClientCallDetails, Any], Awaitable[Any]],
        client_call_details: aio.ClientCallDetails,
        request: Any,
    ) -> Any:
        return await continuation(_with_request_id(client_call_details), request)


class RequestIdStreamUnaryClientInterceptor(aio.StreamUnaryClientInterceptor):  # type: ignore[misc]
    async def intercept_stream_unary(
        self,
        continuation: Callable[[aio.ClientCallDetails, Any], Awaitable[Any]],
        client_call_details: aio.ClientCallDetails,
        request_iterator: Any,
    ) -> Any:
        return await continuation(_with_request_id(client_call_details), request_iterator)


class RequestIdStreamStreamClientInterceptor(aio.StreamStreamClientInterceptor):  # type: ignore[misc]
    async def intercept_stream_stream(
        self,
        continuation: Callable[[aio.ClientCallDetails, Any], Awaitable[Any]],
        client_call_details: aio.ClientCallDetails,
        request_iterator: Any,
    ) -> Any:
        return await continuation(_with_request_id(client_call_details), request_iterator)


def request_id_server_interceptors() -> list[aio.ServerInterceptor]:
    return [RequestIdServerInterceptor()]


def request_id_client_interceptors() -> list[aio.ClientInterceptor]:
    """All four RPC shapes.

    gRPC dispatches to a different interceptor class per shape, so registering
    only the unary-unary one silently dropped the request ID from every
    streaming call.
    """
    return [
        RequestIdClientInterceptor(),
        RequestIdUnaryStreamClientInterceptor(),
        RequestIdStreamUnaryClientInterceptor(),
        RequestIdStreamStreamClientInterceptor(),
    ]
