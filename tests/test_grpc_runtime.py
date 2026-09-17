"""End-to-end gRPC: a real aio server and channel pool, no generated stubs.

Uses a generic handler with pass-through serializers, so these exercise the
actual interceptor chain over a real socket rather than mocks.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import grpc
import pytest
import structlog
from grpc import aio

from py_common.runtime import GrpcChannelPool, GrpcServer

ECHO_METHOD = "/probe.Echo/Call"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


class _Recorder:
    """Captures the metadata and bound context each RPC saw on the server."""

    def __init__(self) -> None:
        self.request_ids: dict[str, str | None] = {}
        self.metadata: dict[str, dict[str, str]] = {}

    def registrar(self, server: aio.Server) -> None:
        async def call(request: bytes, context: grpc.ServicerContext) -> bytes:
            name = request.decode()
            # Sleep so concurrent RPCs genuinely interleave.
            await asyncio.sleep(0.02)
            bound = structlog.contextvars.get_contextvars().get("request_id")
            self.request_ids[name] = str(bound) if bound is not None else None
            self.metadata[name] = {k.lower(): v for k, v in context.invocation_metadata()}
            return request

        handler = grpc.method_handlers_generic_handler(
            "probe.Echo",
            {
                "Call": grpc.unary_unary_rpc_method_handler(
                    call,
                    request_deserializer=lambda b: b,
                    response_serializer=lambda b: b,
                )
            },
        )
        server.add_generic_rpc_handlers((handler,))


@pytest.fixture
async def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
async def address(recorder: _Recorder) -> AsyncIterator[str]:
    port = _free_port()
    server = GrpcServer(
        host="127.0.0.1",
        port=port,
        registrars=[recorder.registrar],
        use_otel_interceptor=False,
    )
    await server.start()
    yield f"127.0.0.1:{port}"
    await server.stop(grace=0)


@pytest.fixture
async def pool() -> AsyncIterator[GrpcChannelPool]:
    pool = GrpcChannelPool(use_otel_interceptor=False)
    yield pool
    await pool.aclose()


async def _call(pool: GrpcChannelPool, address: str, payload: str) -> bytes:
    channel = await pool.get_channel(address)
    result: bytes = await channel.unary_unary(ECHO_METHOD)(payload.encode())
    return result


async def test_request_id_flows_from_client_context_to_server(
    address: str,
    pool: GrpcChannelPool,
    recorder: _Recorder,
) -> None:
    """The caller's request ID must reach the callee without being passed by hand."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id="rid-outbound")

    assert await _call(pool, address, "one") == b"one"

    assert recorder.metadata["one"]["x-request-id"] == "rid-outbound"
    assert recorder.request_ids["one"] == "rid-outbound"
    structlog.contextvars.clear_contextvars()


async def test_server_generates_request_id_when_caller_sends_none(
    address: str,
    pool: GrpcChannelPool,
    recorder: _Recorder,
) -> None:
    structlog.contextvars.clear_contextvars()

    await _call(pool, address, "anon")

    assert "x-request-id" not in recorder.metadata["anon"]
    generated = recorder.request_ids["anon"]
    assert generated and generated != "None"


async def test_concurrent_rpcs_keep_their_own_request_id(
    address: str,
    pool: GrpcChannelPool,
    recorder: _Recorder,
) -> None:
    """Each RPC runs in its own task, so the bound context must not bleed across.

    gRPC aio copies contextvars per RPC task; this pins that behaviour so a
    future change to the interceptor cannot start cross-wiring correlation IDs.
    """

    async def one(index: int) -> None:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=f"rid-{index}")
        await _call(pool, address, f"rpc{index}")

    await asyncio.gather(*(one(i) for i in range(6)))

    assert recorder.request_ids == {f"rpc{i}": f"rid-{i}" for i in range(6)}
    structlog.contextvars.clear_contextvars()


async def test_channel_pool_reuses_one_channel_per_target(
    address: str,
    pool: GrpcChannelPool,
) -> None:
    first = await pool.get_channel(address)
    second = await pool.get_channel(address)
    assert first is second


async def test_disabled_server_does_not_listen() -> None:
    server = GrpcServer(host="127.0.0.1", port=_free_port(), registrars=[], enabled=False)
    await server.start()
    assert server.server is None
    await server.stop(grace=0)


def test_request_id_client_interceptors_cover_every_rpc_shape() -> None:
    """Registering only the unary-unary one silently dropped the ID on streams."""
    from py_common.runtime.grpc_interceptors import request_id_client_interceptors

    interceptors = request_id_client_interceptors()
    bases = {
        aio.UnaryUnaryClientInterceptor,
        aio.UnaryStreamClientInterceptor,
        aio.StreamUnaryClientInterceptor,
        aio.StreamStreamClientInterceptor,
    }
    covered = {base for base in bases if any(isinstance(i, base) for i in interceptors)}
    assert covered == bases


def test_otel_client_interceptors_are_attached_by_default() -> None:
    """The mirror of the server side — without these, outbound calls send no traceparent."""
    from py_common.runtime import default_otel_client_interceptors

    assert default_otel_client_interceptors(), "OTel grpc instrumentation should be installed"

    with_otel = GrpcChannelPool()
    without = GrpcChannelPool(use_otel_interceptor=False)
    assert len(with_otel._interceptors) > len(without._interceptors)
