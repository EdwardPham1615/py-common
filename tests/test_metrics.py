"""RED metrics: the HTTP middleware, the gRPC interceptor, and the scrape endpoint.

Every assertion goes through a real ``InMemoryMetricReader`` rather than a mock
recorder, so these also prove the API-level instruments really do rebind to a
provider installed after import.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator, Iterator
from typing import Any

import grpc
import pytest
from fastapi import FastAPI
from grpc import aio
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import (
    Counter,
    Histogram,
    MeterProvider,
    ObservableCounter,
    ObservableGauge,
    ObservableUpDownCounter,
    UpDownCounter,
)
from opentelemetry.sdk.metrics.export import AggregationTemporality, InMemoryMetricReader
from starlette.testclient import TestClient

from py_common.config import BaseAppSettings, HttpSettings
from py_common.http.middleware import MetricsMiddleware, apply_standard_middleware
from py_common.runtime import GrpcChannelPool, GrpcServer

ECHO_METHOD = "/probe.Probe/Echo"
STREAM_METHOD = "/probe.Probe/Stream"
BOOM_METHOD = "/probe.Probe/Boom"


# Delta temporality so each collection returns only what the current test
# recorded. A per-test provider is not an option: the OTel API installs the
# global provider exactly once per process and ignores later attempts, and the
# proxy instruments bind to their real counterparts on first use.
_DELTA = dict.fromkeys(
    (
        Counter,
        Histogram,
        UpDownCounter,
        ObservableCounter,
        ObservableUpDownCounter,
        ObservableGauge,
    ),
    AggregationTemporality.DELTA,
)


@pytest.fixture(scope="session")
def _provider_reader() -> InMemoryMetricReader:
    reader = InMemoryMetricReader(preferred_temporality=_DELTA)  # type: ignore[arg-type]
    otel_metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    return reader


@pytest.fixture
def reader(_provider_reader: InMemoryMetricReader) -> Iterator[InMemoryMetricReader]:
    _provider_reader.get_metrics_data()  # drain whatever earlier tests recorded
    yield _provider_reader


def points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [
        point
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def attrs_of(point: Any) -> dict[str, Any]:
    return dict(point.attributes)


# --------------------------------------------------------------------------- HTTP


def build_app(**middleware_kwargs: Any) -> FastAPI:
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ok"}

    apply_standard_middleware(app, BaseAppSettings(), **middleware_kwargs)
    return app


def test_duration_uses_the_route_template_not_the_raw_path(
    reader: InMemoryMetricReader,
) -> None:
    client = TestClient(build_app())
    client.get("/items/abc")
    client.get("/items/def")

    recorded = points(reader, "http.server.request.duration")
    assert len(recorded) == 1, "both requests must land on one series, not one per id"
    attributes = attrs_of(recorded[0])
    assert attributes["http.route"] == "/items/{item_id}"
    assert attributes["http.request.method"] == "GET"
    assert attributes["http.response.status_code"] == 200
    assert recorded[0].count == 2
    assert recorded[0].sum > 0


def test_unmatched_path_carries_no_route_attribute(reader: InMemoryMetricReader) -> None:
    client = TestClient(build_app())
    client.get("/no/such/thing")

    recorded = points(reader, "http.server.request.duration")
    assert len(recorded) == 1
    attributes = attrs_of(recorded[0])
    assert attributes["http.response.status_code"] == 404
    # The path is attacker-controlled; recording it would let anyone mint series.
    assert "http.route" not in attributes


def test_unknown_method_collapses_to_other(reader: InMemoryMetricReader) -> None:
    client = TestClient(build_app())
    client.request("PROPFIND", "/items/abc")

    recorded = points(reader, "http.server.request.duration")
    assert [attrs_of(p)["http.request.method"] for p in recorded] == ["_OTHER"]


def test_unhandled_exception_is_counted_as_a_500(reader: InMemoryMetricReader) -> None:
    client = TestClient(build_app(), raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 500

    recorded = points(reader, "http.server.request.duration")
    assert len(recorded) == 1
    attributes = attrs_of(recorded[0])
    assert attributes["http.response.status_code"] == 500
    assert attributes["http.route"] == "/boom"


def test_probe_traffic_is_excluded(reader: InMemoryMetricReader) -> None:
    client = TestClient(build_app())
    client.get("/health/ready")

    assert points(reader, "http.server.request.duration") == []


def test_active_requests_returns_to_zero(reader: InMemoryMetricReader) -> None:
    client = TestClient(build_app())
    client.get("/items/abc")

    recorded = points(reader, "http.server.active_requests")
    assert len(recorded) == 1
    assert recorded[0].value == 0


def test_active_requests_returns_to_zero_after_a_failure(reader: InMemoryMetricReader) -> None:
    """A leaked increment here would make the gauge climb forever."""
    app = FastAPI()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    app.add_middleware(MetricsMiddleware)
    client = TestClient(app, raise_server_exceptions=False)
    client.get("/boom")

    recorded = points(reader, "http.server.active_requests")
    assert len(recorded) == 1
    assert recorded[0].value == 0


def test_metrics_can_be_turned_off(reader: InMemoryMetricReader) -> None:
    client = TestClient(build_app(metrics=False))
    client.get("/items/abc")

    assert points(reader, "http.server.request.duration") == []


# ----------------------------------------------------------------------- /metrics


def test_scrape_endpoint_reports_503_until_metrics_are_set_up() -> None:
    from py_common.telemetry import build_metrics_router

    app = FastAPI()
    app.include_router(build_metrics_router())
    response = TestClient(app).get("/metrics")

    assert response.status_code == 503
    assert "setup_metrics" in response.text


@pytest.fixture
def isolated_setup() -> Iterator[Any]:
    """Let a test run the real ``setup_metrics`` without leaking its globals.

    Only the module state is restored, not the global MeterProvider — the OTel
    API pins that for the process, so the provider ``setup_metrics`` builds here
    is inert and the session reader keeps serving every other test.
    """
    from py_common.telemetry import metrics as metrics_module

    yield metrics_module
    metrics_module._provider = None
    metrics_module._prometheus_registry = None


def test_setup_metrics_returns_nothing_when_disabled(isolated_setup: Any) -> None:
    assert isolated_setup.setup_metrics(service_name="probe", enabled=False) is None
    assert isolated_setup._prometheus_registry is None


def test_setup_metrics_is_idempotent(isolated_setup: Any) -> None:
    first = isolated_setup.setup_metrics(service_name="probe", otlp=False, prometheus=True)
    second = isolated_setup.setup_metrics(service_name="other", otlp=False, prometheus=True)

    assert first is not None
    assert second is first, "a second call must not rebuild the export pipeline"


def test_scrape_endpoint_serves_prometheus_text(isolated_setup: Any) -> None:
    from py_common.telemetry import build_metrics_router

    provider = isolated_setup.setup_metrics(service_name="probe", otlp=False, prometheus=True)
    # Record through this provider's own meter: the process-global one is
    # already pinned to the session reader, so the module-level instruments
    # would never reach the registry under test.
    provider.get_meter("probe").create_histogram("http.server.request.duration", unit="s").record(
        0.01, {"http.route": "/items/{item_id}"}
    )

    app = FastAPI()
    app.include_router(build_metrics_router())
    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "http_server_request_duration_seconds" in response.text
    assert 'http_route="/items/{item_id}"' in response.text


def test_shutdown_metrics_clears_the_scrape_registry(isolated_setup: Any) -> None:
    isolated_setup.setup_metrics(service_name="probe", otlp=False, prometheus=True)
    assert isolated_setup._prometheus_registry is not None

    isolated_setup.shutdown_metrics()

    assert isolated_setup._provider is None
    assert isolated_setup._prometheus_registry is None


# --------------------------------------------------------------------------- gRPC


async def echo(request: bytes, context: aio.ServicerContext) -> bytes:
    return request


async def stream(request: bytes, context: aio.ServicerContext) -> AsyncIterator[bytes]:
    for _ in range(3):
        yield request


async def boom(request: bytes, context: aio.ServicerContext) -> bytes:
    await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "nope")
    raise AssertionError("unreachable")


def register_probe(server: aio.Server) -> None:
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "probe.Probe",
                {
                    "Echo": grpc.unary_unary_rpc_method_handler(echo),
                    "Stream": grpc.unary_stream_rpc_method_handler(stream),
                    "Boom": grpc.unary_unary_rpc_method_handler(boom),
                },
            ),
        )
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.asyncio
async def test_grpc_server_records_rpc_duration(reader: InMemoryMetricReader) -> None:
    port = free_port()
    server = GrpcServer(host="127.0.0.1", port=port, registrars=[register_probe])
    await server.start()
    pool = GrpcChannelPool()
    try:
        channel = await pool.get_channel(f"127.0.0.1:{port}")
        await channel.unary_unary(ECHO_METHOD)(b"hi")

        stream_call = channel.unary_stream(STREAM_METHOD)(b"hi")
        assert [msg async for msg in stream_call] == [b"hi"] * 3

        with pytest.raises(grpc.RpcError):
            await channel.unary_unary(BOOM_METHOD)(b"hi")
    finally:
        await pool.aclose()
        await server.stop(grace=0)

    recorded = {
        attrs_of(p)["rpc.method"]: attrs_of(p) for p in points(reader, "rpc.server.duration")
    }
    assert set(recorded) == {"Echo", "Stream", "Boom"}
    assert recorded["Echo"]["rpc.system"] == "grpc"
    assert recorded["Echo"]["rpc.service"] == "probe.Probe"
    assert recorded["Echo"]["rpc.grpc.status_code"] == "OK"
    assert recorded["Stream"]["rpc.grpc.status_code"] == "OK"
    assert recorded["Boom"]["rpc.grpc.status_code"] == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_streaming_duration_covers_the_whole_stream(reader: InMemoryMetricReader) -> None:
    """Timing ``continuation`` instead of the behavior would report ~0 here."""
    import anyio

    async def slow_stream(request: bytes, context: aio.ServicerContext) -> AsyncIterator[bytes]:
        for _ in range(2):
            await anyio.sleep(0.05)
            yield request

    def register(server: aio.Server) -> None:
        server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    "probe.Probe",
                    {"Stream": grpc.unary_stream_rpc_method_handler(slow_stream)},
                ),
            )
        )

    port = free_port()
    server = GrpcServer(host="127.0.0.1", port=port, registrars=[register])
    await server.start()
    pool = GrpcChannelPool()
    try:
        channel = await pool.get_channel(f"127.0.0.1:{port}")
        call = channel.unary_stream(STREAM_METHOD)(b"hi")
        assert [msg async for msg in call] == [b"hi"] * 2
    finally:
        await pool.aclose()
        await server.stop(grace=0)

    recorded = points(reader, "rpc.server.duration")
    assert len(recorded) == 1
    assert recorded[0].sum >= 100, "must cover both sleeps, not just handler lookup"


def test_a_timed_out_request_is_counted_as_504(reader: InMemoryMetricReader) -> None:
    """TimeoutMiddleware sits inside the metrics layer, so a request it abandons
    is recorded like any other response rather than vanishing from the rate."""
    import anyio

    app = FastAPI()

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        await anyio.sleep(5)
        return {}

    apply_standard_middleware(app, BaseAppSettings(http=HttpSettings(timeout_seconds=0.1)))
    assert TestClient(app).get("/slow").status_code == 504

    statuses = {
        attrs_of(p).get("http.response.status_code")
        for p in points(reader, "http.server.request.duration")
    }
    assert 504 in statuses
