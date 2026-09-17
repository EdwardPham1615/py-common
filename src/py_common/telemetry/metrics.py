"""OpenTelemetry metrics: a MeterProvider, RED instruments, and ``/metrics``.

Traces answer "what happened in *this* request". Metrics answer "what fraction
of *all* requests failed, and how slow are they" — the question an alert asks.
Until this module existed :mod:`py_common.telemetry` set up traces only, so a
service could be debugged one request at a time but could not be alerted on.

The instruments here are created from the OTel *API*, not the SDK, at import
time. With no provider configured they are no-ops that cost a function call, so
importing this module never forces the SDK on a caller; when
:func:`setup_metrics` later installs a real ``MeterProvider`` the API rebinds
them in place.

Two export paths, either or both:

* **OTLP push** (default) — the same collector the spans already go to.
* **Prometheus pull** — a ``/metrics`` endpoint via :func:`build_metrics_router`,
  for clusters that scrape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opentelemetry import metrics

from py_common.logging import get_logger

if TYPE_CHECKING:
    from fastapi import APIRouter

logger = get_logger(__name__)

__all__ = [
    "HTTP_DURATION_BUCKETS_SECONDS",
    "RPC_DURATION_BUCKETS_MS",
    "build_metrics_router",
    "http_server_active_requests",
    "http_server_duration",
    "rpc_server_duration",
    "setup_metrics",
    "shutdown_metrics",
]

# Bucket boundaries from the OTel HTTP semantic conventions. Declaring them as
# an advisory (rather than an SDK View) keeps them attached to the instrument,
# so any provider — ours or the caller's — gets latency buckets that match what
# published dashboards and alert rules expect.
HTTP_DURATION_BUCKETS_SECONDS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1.0,
    2.5,
    5.0,
    7.5,
    10.0,
)

# The RPC conventions measure in milliseconds, not seconds.
RPC_DURATION_BUCKETS_MS = (1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 5000.0)

_meter = metrics.get_meter("py_common")

http_server_duration = _meter.create_histogram(
    "http.server.request.duration",
    unit="s",
    description="Duration of inbound HTTP requests.",
    explicit_bucket_boundaries_advisory=HTTP_DURATION_BUCKETS_SECONDS,
)
"""Rate + Error + Duration for HTTP in one instrument: the count gives rate, the
``http.response.status_code`` attribute gives the error ratio, the histogram
gives latency."""

http_server_active_requests = _meter.create_up_down_counter(
    "http.server.active_requests",
    unit="{request}",
    description="Inbound HTTP requests currently being handled.",
)

rpc_server_duration = _meter.create_histogram(
    "rpc.server.duration",
    unit="ms",
    description="Duration of inbound gRPC calls.",
    explicit_bucket_boundaries_advisory=RPC_DURATION_BUCKETS_MS,
)

_provider: Any = None
_prometheus_registry: Any = None


def setup_metrics(
    *,
    service_name: str,
    otlp_endpoint: str = "http://localhost:4317",
    insecure: bool = True,
    enabled: bool = True,
    environment: str = "dev",
    export_interval_ms: int = 60_000,
    otlp: bool = True,
    prometheus: bool = False,
) -> Any:
    """Install a ``MeterProvider`` and return it (``None`` when disabled).

    Deliberately takes no ``app``: workers, gRPC servers and CLIs need metrics
    too, and none of them have a FastAPI instance. :func:`setup_telemetry` calls
    this for HTTP services.

    As with the tracer provider, the global provider is created once per process
    and later calls reuse it — a second call with different exporter settings
    warns rather than silently rebuilding the pipeline.
    """
    global _provider, _prometheus_registry
    if not enabled:
        return None

    if _provider is not None:
        logger.warning(
            "metrics_already_initialized",
            detail="MeterProvider already set; reusing it and ignoring new exporter settings",
        )
        return _provider

    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import MetricReader
    from opentelemetry.sdk.resources import Resource

    readers: list[MetricReader] = []
    if otlp:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=otlp_endpoint, insecure=insecure),
                export_interval_millis=export_interval_ms,
            )
        )
    if prometheus:
        try:
            from opentelemetry.exporter.prometheus import PrometheusMetricReader
            from prometheus_client import CollectorRegistry

            # A dedicated registry rather than prometheus_client's global one:
            # the global registry raises on duplicate collectors, which turns a
            # second setup_metrics() (tests, reload) into a hard crash.
            _prometheus_registry = CollectorRegistry()
            readers.append(PrometheusMetricReader(registry=_prometheus_registry))
        except ImportError:
            logger.warning(
                "prometheus_exporter_unavailable",
                detail="install py-common[telemetry] to serve /metrics",
            )

    if not readers:
        logger.warning("metrics_no_readers", detail="metrics enabled but no exporter configured")

    _provider = MeterProvider(
        resource=Resource.create(
            {"service.name": service_name, "deployment.environment": environment}
        ),
        metric_readers=readers,
    )
    metrics.set_meter_provider(_provider)
    return _provider


def shutdown_metrics() -> None:
    """Flush and shut down the meter provider (call from app shutdown/lifespan).

    Without this the last export interval's measurements are lost, which is
    exactly the window where a crashing pod's metrics matter most.
    """
    global _provider, _prometheus_registry
    if _provider is None:
        return
    try:
        _provider.shutdown()
    except Exception:
        logger.exception("metrics_shutdown_failed")
    finally:
        _provider = None
        _prometheus_registry = None


def build_metrics_router(*, path: str = "/metrics", include_in_schema: bool = False) -> APIRouter:
    """Router exposing the Prometheus scrape endpoint at ``path``.

    Requires ``setup_metrics(prometheus=True)``. The registry is resolved per
    request rather than at build time so the router can be mounted by an app
    factory before telemetry is initialized in the lifespan; until it is, the
    endpoint answers 503 instead of quietly serving an empty page that would
    make a scraper report "up" for a service exporting nothing.
    """
    from fastapi import APIRouter, Response, status

    router = APIRouter(tags=["metrics"])

    @router.get(path, include_in_schema=include_in_schema)
    async def scrape() -> Response:
        if _prometheus_registry is None:
            return Response(
                "metrics not initialized: call setup_metrics(prometheus=True)",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                media_type="text/plain",
            )
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        return Response(generate_latest(_prometheus_registry), media_type=CONTENT_TYPE_LATEST)

    return router
