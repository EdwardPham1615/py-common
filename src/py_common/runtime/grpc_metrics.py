"""RED metrics for inbound gRPC calls.

The HTTP side gets its metrics from an ASGI middleware; gRPC has no such layer,
so the equivalent lives in a server interceptor. Without it a service that
speaks gRPC is invisible to rate/error alerts even when its HTTP surface is
fully covered — the same asymmetry that once left outbound gRPC untraced.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import grpc
from grpc import aio

from py_common.runtime._grpc_behavior import behavior_field
from py_common.telemetry.metrics import rpc_server_duration

__all__ = ["MetricsServerInterceptor", "metrics_server_interceptors"]


def _split_method(full_method: str) -> tuple[str, str]:
    """``/pkg.Service/Method`` -> ``("pkg.Service", "Method")``."""
    service, _, method = full_method.lstrip("/").partition("/")
    return service or "unknown", method or "unknown"


def _status_name(context: aio.ServicerContext) -> str:
    """The gRPC status the handler ended with; ``OK`` when it never set one."""
    try:
        code = context.code()
    except Exception:
        return str(grpc.StatusCode.UNKNOWN.name)
    if code is None:
        return str(grpc.StatusCode.OK.name)
    return str(getattr(code, "name", code))


class MetricsServerInterceptor(aio.ServerInterceptor):  # type: ignore[misc]
    """Record ``rpc.server.duration`` for every inbound call.

    Attributes follow the OTel RPC conventions — ``rpc.system``, ``rpc.service``,
    ``rpc.method``, ``rpc.grpc.status_code`` — all bounded by the service's own
    proto definitions, so cardinality is fixed at build time.

    The duration covers the handler, including the time a streaming handler
    spends producing messages: the interceptor wraps the *behavior* rather than
    timing ``continuation``, which returns as soon as the handler is looked up
    and would otherwise report every RPC as taking microseconds.
    """

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[Any]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> Any:
        handler = await continuation(handler_call_details)
        if handler is None:
            return None

        field = behavior_field(handler)
        if field is None:
            return handler
        behavior = getattr(handler, field)

        service, method = _split_method(str(handler_call_details.method))
        base = {"rpc.system": "grpc", "rpc.service": service, "rpc.method": method}

        def record(start: float, context: aio.ServicerContext, error: BaseException | None) -> None:
            attrs: dict[str, Any] = {**base, "rpc.grpc.status_code": _status_name(context)}
            if error is not None:
                attrs["error.type"] = type(error).__qualname__
            rpc_server_duration.record((time.perf_counter() - start) * 1000, attrs)

        if handler.response_streaming:

            async def stream_behavior(request: Any, context: Any) -> AsyncIterator[Any]:
                start = time.perf_counter()
                error: BaseException | None = None
                try:
                    result = behavior(request, context)
                    # A response-streaming handler may be an async generator
                    # function or a coroutine that returns an async iterable;
                    # gRPC accepts both, so both have to be unwrapped here.
                    if inspect.isawaitable(result):
                        result = await result
                    async for message in result:
                        yield message
                except BaseException as exc:
                    error = exc
                    raise
                finally:
                    record(start, context, error)

            return handler._replace(**{field: stream_behavior})

        async def unary_behavior(request: Any, context: Any) -> Any:
            start = time.perf_counter()
            error: BaseException | None = None
            try:
                return await behavior(request, context)
            except BaseException as exc:
                error = exc
                raise
            finally:
                record(start, context, error)

        return handler._replace(**{field: unary_behavior})


def metrics_server_interceptors() -> list[aio.ServerInterceptor]:
    return [MetricsServerInterceptor()]
