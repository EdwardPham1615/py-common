"""OpenTelemetry bootstrap: traces for any process, FastAPI instrumentation on top."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from py_common.logging import get_logger
from py_common.telemetry.metrics import (
    build_metrics_router,
    setup_metrics,
    shutdown_metrics,
)
from py_common.telemetry.profiler import enable_profiler

if TYPE_CHECKING:
    from fastapi import FastAPI
    from opentelemetry.sdk.trace import TracerProvider

logger = get_logger(__name__)

_provider: TracerProvider | None = None

__all__ = [
    "build_metrics_router",
    "enable_profiler",
    "instrument_sqlalchemy",
    "setup_metrics",
    "setup_telemetry",
    "setup_tracing",
    "shutdown_metrics",
    "shutdown_telemetry",
    "shutdown_tracing",
]

# Client libraries that can be traced without an app object. Name -> (module, class).
_CLIENT_INSTRUMENTORS: dict[str, tuple[str, str]] = {
    "httpx": ("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor"),
    "redis": ("opentelemetry.instrumentation.redis", "RedisInstrumentor"),
    "pymongo": ("opentelemetry.instrumentation.pymongo", "PymongoInstrumentor"),
    "celery": ("opentelemetry.instrumentation.celery", "CeleryInstrumentor"),
}
DEFAULT_INSTRUMENT: tuple[str, ...] = tuple(_CLIENT_INSTRUMENTORS)


def setup_tracing(
    *,
    service_name: str,
    otlp_endpoint: str = "http://localhost:4317",
    insecure: bool = True,
    sampler_arg: float = 1.0,
    environment: str = "dev",
    enabled: bool = True,
    instrument: Sequence[str] = DEFAULT_INSTRUMENT,
    redact_query_params: Sequence[str] = (),
) -> TracerProvider | None:
    """Create the process's ``TracerProvider`` and trace its client libraries.

    This is everything tracing needs that does not involve a web app, so a Celery
    worker, a beat scheduler or a one-off script gets the same pipeline a FastAPI
    service does. It exports to ``otlp_endpoint`` over gRPC, samples with
    ``ParentBasedTraceIdRatio(sampler_arg)``, and turns on the instrumentors named
    in ``instrument`` (any of ``httpx``, ``redis``, ``pymongo``, ``celery``; each is
    skipped quietly when its library is not installed).

    Returns the provider, or ``None`` when ``enabled`` is false. One provider per
    process: a second call reuses it, logs a warning, and ignores the new exporter
    settings.

    **Call it in the process that does the work, after any fork.** The batch span
    processor owns a thread, and a thread does not survive ``fork()``: a provider
    built in a parent and inherited by a prefork child exports nothing, silently.
    A Celery worker therefore calls this from ``worker_process_init``, not at import.
    Call :func:`shutdown_tracing` when the process stops so buffered spans flush.

    Secrets are scrubbed from exported spans (see
    :class:`~py_common.telemetry.redact.RedactingSpanExporter`): credential-named
    query parameters in URLs, plus any names in ``redact_query_params``, and
    ``Authorization`` / ``Cookie`` headers if an instrumentor captures them.
    """
    global _provider
    unknown = sorted(set(instrument) - set(_CLIENT_INSTRUMENTORS))
    if unknown:
        raise ValueError(
            f"unknown instrument name(s) {unknown}; expected any of {sorted(_CLIENT_INSTRUMENTORS)}"
        )
    if not enabled:
        return None

    # Imported here rather than at module scope. `opentelemetry.exporter` and
    # the SDK belong to the `telemetry` extra, but this file is executed
    # whenever anything touches `py_common.telemetry.metrics` -- which the
    # HTTP and gRPC metrics middleware do, from extras that never asked for
    # the SDK. metrics.py's own docstring promises importing it "never
    # forces the SDK on a caller"; this is what keeps that true. The
    # instrumentor imports below already worked this way.
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio

    from py_common.telemetry.redact import RedactingSpanExporter

    if _provider is None:
        resource = Resource.create(
            {
                "service.name": service_name,
                "deployment.environment": environment,
            }
        )
        _provider = TracerProvider(
            resource=resource,
            sampler=ParentBasedTraceIdRatio(sampler_arg),
        )
        # The redactor wraps the exporter: the last point a span can be rewritten.
        exporter = RedactingSpanExporter(
            OTLPSpanExporter(endpoint=otlp_endpoint, insecure=insecure), redact_query_params
        )
        _provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(_provider)
    else:
        logger.warning(
            "telemetry_already_initialized",
            detail="TracerProvider already set; reusing it and ignoring new exporter settings",
        )

    _instrument_clients(instrument)
    return _provider


def setup_telemetry(
    app: FastAPI,
    *,
    service_name: str,
    otlp_endpoint: str = "http://localhost:4317",
    insecure: bool = True,
    sampler_arg: float = 1.0,
    enabled: bool = True,
    environment: str = "dev",
    metrics_enabled: bool = True,
    metrics_export_interval_ms: int = 60_000,
    prometheus_metrics: bool = False,
    redact_query_params: Sequence[str] = (),
) -> TracerProvider | None:
    """Initialize the OTel SDK and instrument FastAPI + common client libraries.

    :func:`setup_tracing` plus the FastAPI app and metrics. Returns the active
    ``TracerProvider`` (or ``None`` when disabled). The global provider is created
    once per process; subsequent calls reuse it and only instrument the given app.
    Call :func:`shutdown_telemetry` on app shutdown to flush buffered spans.

    Metrics are set up alongside traces (``metrics_enabled``) and pushed to the
    same OTLP endpoint. Set ``prometheus_metrics=True`` to also serve a scrape
    endpoint — then mount :func:`build_metrics_router`. Processes without a
    FastAPI app (workers, gRPC servers) call :func:`setup_tracing` and
    :func:`~py_common.telemetry.metrics.setup_metrics` directly.
    """
    if not enabled:
        return None

    setup_metrics(
        service_name=service_name,
        otlp_endpoint=otlp_endpoint,
        insecure=insecure,
        enabled=metrics_enabled,
        environment=environment,
        export_interval_ms=metrics_export_interval_ms,
        prometheus=prometheus_metrics,
    )
    provider = setup_tracing(
        service_name=service_name,
        otlp_endpoint=otlp_endpoint,
        insecure=insecure,
        sampler_arg=sampler_arg,
        environment=environment,
        redact_query_params=redact_query_params,
    )
    _instrument_fastapi(app)
    return provider


def shutdown_tracing() -> None:
    """Flush buffered spans and shut the tracer provider down (call as the process stops)."""
    global _provider
    if _provider is None:
        return
    try:
        _provider.shutdown()
    except Exception:
        logger.exception("telemetry_shutdown_failed")
    finally:
        _provider = None


def shutdown_telemetry() -> None:
    """Flush and shut down the tracer *and* meter providers (call from app shutdown)."""
    shutdown_metrics()
    shutdown_tracing()


def _instrument_fastapi(app: FastAPI) -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        if not getattr(app.state, "_otel_fastapi_instrumented", False):
            FastAPIInstrumentor.instrument_app(app, excluded_urls="health,ready,live")
            app.state._otel_fastapi_instrumented = True
    except ImportError:
        logger.warning("otel_instrumentor_unavailable", instrumentor="fastapi")
    except Exception:
        logger.exception("otel_instrumentation_failed", instrumentor="fastapi")


def _instrument_clients(names: Sequence[str]) -> None:
    for name in names:
        mod_path, attr = _CLIENT_INSTRUMENTORS[name]
        try:
            module = __import__(mod_path, fromlist=[attr])
            instrumentor = getattr(module, attr)()
            if not getattr(instrumentor, "is_instrumented_by_opentelemetry", False):
                instrumentor.instrument()
        except ImportError:
            # Optional instrumentation — the target library isn't installed.
            pass
        except Exception:
            logger.exception("otel_instrumentation_failed", instrumentor=attr)


def instrument_sqlalchemy(engine: Any) -> None:
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    except ImportError:
        logger.warning("otel_instrumentor_unavailable", instrumentor="sqlalchemy")
    except Exception:
        logger.exception("otel_instrumentation_failed", instrumentor="sqlalchemy")
