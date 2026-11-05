"""setup_telemetry / shutdown_telemetry control flow.

These exercise the wiring without installing a real SDK pipeline: the tracer
provider is process-global, and a test that sets it for real would leak into
every other test in the run.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio

import py_common.telemetry as tel


@pytest.fixture(autouse=True)
def _reset_provider() -> Any:
    original = tel._provider
    yield
    tel._provider = original


def test_disabled_sets_up_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``enabled=False`` must be inert — no provider, and no metrics pipeline
    either. A service that opts out should not open exporter connections."""
    called: list[str] = []
    monkeypatch.setattr(tel, "setup_metrics", lambda **kw: called.append("metrics"))
    monkeypatch.setattr(tel, "_instrument_fastapi", lambda app: called.append("instrument"))
    monkeypatch.setattr(tel, "_instrument_clients", lambda names: called.append("clients"))
    tel._provider = None

    assert tel.setup_telemetry(None, service_name="svc", enabled=False) is None  # type: ignore[arg-type]
    assert called == []
    assert tel._provider is None


def test_second_call_reuses_provider_and_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reusing the provider is right — but silently ignoring new exporter settings
    would leave someone debugging why their endpoint change had no effect."""
    monkeypatch.setattr(tel, "setup_metrics", lambda **kw: None)
    monkeypatch.setattr(tel, "_instrument_clients", lambda names: None)
    monkeypatch.setattr(tel, "_instrument_fastapi", lambda app: None)
    warnings: list[str] = []
    monkeypatch.setattr(tel.logger, "warning", lambda event, **kw: warnings.append(event))

    sentinel = object()
    tel._provider = sentinel  # type: ignore[assignment]

    result = tel.setup_telemetry(None, service_name="svc", otlp_endpoint="http://other:4317")  # type: ignore[arg-type]
    assert result is sentinel
    assert warnings == ["telemetry_already_initialized"]


def test_shutdown_without_provider_still_flushes_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(tel, "shutdown_metrics", lambda: called.append("metrics"))
    tel._provider = None

    tel.shutdown_telemetry()
    assert called == ["metrics"]


def test_shutdown_survives_a_failing_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown runs while the process is already going down. A raising exporter
    must not stop the rest of shutdown, and must not leave a dead provider
    installed for whatever runs next."""
    monkeypatch.setattr(tel, "shutdown_metrics", lambda: None)
    logged: list[str] = []
    monkeypatch.setattr(tel.logger, "exception", lambda event, **kw: logged.append(event))

    class _Boom:
        def shutdown(self) -> None:
            raise RuntimeError("exporter is gone")

    tel._provider = _Boom()  # type: ignore[assignment]
    tel.shutdown_telemetry()

    assert logged == ["telemetry_shutdown_failed"]
    assert tel._provider is None


# --- first-call provider construction -------------------------------------
#
# The branch that actually builds the pipeline. It is exercised with the SDK
# pieces stubbed: constructing a real BatchSpanProcessor starts an exporter
# thread, and calling the real trace.set_tracer_provider would pin a provider
# for the whole process -- OTel refuses to replace one once set, so a single
# test doing it for real would dictate the tracer every later test sees.


class _FakeExporter:
    instances: ClassVar[list[_FakeExporter]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        _FakeExporter.instances.append(self)


class _FakeProcessor:
    """Enough of the SpanProcessor interface for TracerProvider's atexit hook.

    The provider registers shutdown at exit; a processor without these methods
    turns every run into a wall of ignored AttributeErrors after the summary.
    """

    def __init__(self, exporter: Any) -> None:
        self.exporter = exporter

    def on_start(self, span: Any, parent_context: Any = None) -> None: ...

    def on_end(self, span: Any) -> None: ...

    def shutdown(self) -> None: ...

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


@pytest.fixture
def stub_sdk(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build a real TracerProvider, but never export and never set it globally."""
    _FakeExporter.instances.clear()
    installed: list[Any] = []
    # Patched where the symbols live, not on py_common.telemetry. setup_telemetry
    # imports the OTel SDK inside its own body so that importing this package
    # never drags the SDK in -- see the comment there -- which means there is no
    # module-level name to replace, and patching the source module is what the
    # call-time import actually picks up.
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter",
        _FakeExporter,
    )
    monkeypatch.setattr("opentelemetry.sdk.trace.export.BatchSpanProcessor", _FakeProcessor)
    monkeypatch.setattr(tel.trace, "set_tracer_provider", installed.append)
    monkeypatch.setattr(tel, "setup_metrics", lambda **kw: None)
    monkeypatch.setattr(tel, "_instrument_clients", lambda names: None)
    monkeypatch.setattr(tel, "_instrument_fastapi", lambda app: None)
    tel._provider = None
    return installed


def test_first_call_builds_and_installs_the_provider(stub_sdk: list[Any]) -> None:
    provider = tel.setup_telemetry(
        None,  # type: ignore[arg-type]
        service_name="orders",
        environment="staging",
        otlp_endpoint="http://collector:4317",
        insecure=False,
        sampler_arg=0.25,
    )

    assert provider is tel._provider
    assert stub_sdk == [provider]

    attributes = provider.resource.attributes  # type: ignore[union-attr]
    assert attributes["service.name"] == "orders"
    assert attributes["deployment.environment"] == "staging"

    (exporter,) = _FakeExporter.instances
    assert exporter.kwargs == {"endpoint": "http://collector:4317", "insecure": False}


def test_setup_tracing_needs_no_app(stub_sdk: list[Any]) -> None:
    """The point of the split: a worker or a script has no FastAPI app to hand over."""
    provider = tel.setup_tracing(service_name="worker", environment="prod")

    assert provider is tel._provider
    assert stub_sdk == [provider]
    assert provider.resource.attributes["service.name"] == "worker"  # type: ignore[union-attr]


def test_setup_tracing_instruments_only_the_clients_asked_for(
    monkeypatch: pytest.MonkeyPatch, stub_sdk: list[Any]
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(tel, "_instrument_clients", lambda names: seen.append(tuple(names)))

    tel.setup_tracing(service_name="svc", instrument=("httpx", "celery"))

    assert seen == [("httpx", "celery")]


def test_setup_tracing_rejects_a_misspelt_instrumentor(stub_sdk: list[Any]) -> None:
    """A typo would otherwise skip the instrumentor and trace nothing, silently."""
    with pytest.raises(ValueError, match=r"unknown instrument.*'rediss'"):
        tel.setup_tracing(service_name="svc", instrument=("rediss",))
    assert tel._provider is None


def test_setup_tracing_disabled_is_inert(stub_sdk: list[Any]) -> None:
    assert tel.setup_tracing(service_name="svc", enabled=False) is None
    assert tel._provider is None
    assert stub_sdk == []


def test_setup_telemetry_is_setup_tracing_plus_the_app(
    monkeypatch: pytest.MonkeyPatch, stub_sdk: list[Any]
) -> None:
    order: list[str] = []
    monkeypatch.setattr(tel, "setup_metrics", lambda **kw: order.append("metrics"))
    monkeypatch.setattr(tel, "_instrument_clients", lambda names: order.append("clients"))
    monkeypatch.setattr(tel, "_instrument_fastapi", lambda app: order.append("fastapi"))

    tel.setup_telemetry(None, service_name="svc")  # type: ignore[arg-type]

    assert order == ["metrics", "clients", "fastapi"]


def test_shutdown_tracing_shuts_the_provider_down_and_forgets_it() -> None:
    shut: list[bool] = []

    class _Provider:
        def shutdown(self) -> None:
            shut.append(True)

    tel._provider = _Provider()  # type: ignore[assignment]
    tel.shutdown_tracing()

    assert shut == [True]
    assert tel._provider is None
    tel.shutdown_tracing()  # a second call, or one with nothing to do, is fine
    assert shut == [True]


def test_setup_tracing_exports_through_the_redactor(stub_sdk: list[Any]) -> None:
    """The exporter the batch processor drives is the redacting wrapper, around the
    real OTLP one. Skip the wrapper and every URL goes out with its query string."""
    from py_common.telemetry.redact import RedactingSpanExporter

    provider = tel.setup_tracing(service_name="svc", redact_query_params=["session_id"])

    (processor,) = provider._active_span_processor._span_processors  # type: ignore[union-attr]
    assert isinstance(processor, _FakeProcessor)
    assert isinstance(processor.exporter, RedactingSpanExporter)
    assert isinstance(processor.exporter._inner, _FakeExporter)
    assert processor.exporter.redact_url("?session_id=1") == "?session_id=REDACTED"


def test_sampler_ratio_is_applied(stub_sdk: list[Any]) -> None:
    """A service that asked for 5% must not silently export everything."""
    provider = tel.setup_telemetry(
        None,  # type: ignore[arg-type]
        service_name="svc",
        sampler_arg=0.05,
    )

    assert isinstance(provider.sampler, ParentBasedTraceIdRatio)  # type: ignore[union-attr]
    assert "0.05" in provider.sampler.get_description()  # type: ignore[union-attr]


def test_metrics_are_set_up_with_the_same_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Traces and metrics go to one collector; drifting endpoints send half the
    signal somewhere nobody is looking."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(tel, "setup_metrics", lambda **kw: captured.update(kw))
    monkeypatch.setattr(tel, "_instrument_clients", lambda names: None)
    monkeypatch.setattr(tel, "_instrument_fastapi", lambda app: None)
    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter",
        _FakeExporter,
    )
    monkeypatch.setattr("opentelemetry.sdk.trace.export.BatchSpanProcessor", _FakeProcessor)
    monkeypatch.setattr(tel.trace, "set_tracer_provider", lambda p: None)
    tel._provider = None

    tel.setup_telemetry(
        None,  # type: ignore[arg-type]
        service_name="svc",
        otlp_endpoint="http://collector:4317",
        insecure=False,
        environment="prod",
        metrics_enabled=False,
        metrics_export_interval_ms=15_000,
        prometheus_metrics=True,
    )

    assert captured == {
        "service_name": "svc",
        "otlp_endpoint": "http://collector:4317",
        "insecure": False,
        "enabled": False,
        "environment": "prod",
        "export_interval_ms": 15_000,
        "prometheus": True,
    }


# --- instrumentation ------------------------------------------------------
#
# Every branch here is an error branch by design: instrumentation is a nice to
# have, and a missing or broken instrumentor must never stop a service from
# starting. The classes are replaced with doubles -- calling the real
# .instrument() patches httpx, redis and celery process-wide, which would then
# apply to every other test in the run.


class _FakeInstrumentor:
    def __init__(self) -> None:
        self.instrumented = False
        self.is_instrumented_by_opentelemetry = False
        self.kwargs: dict[str, Any] = {}

    def instrument(self, **kwargs: Any) -> None:
        self.instrumented = True
        self.kwargs = kwargs


def _app() -> Any:
    from fastapi import FastAPI

    return FastAPI()


def test_fastapi_is_instrumented_once_per_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Instrumenting the same app twice would produce duplicate spans per request.

    setup_telemetry is called once per process, but the app object survives a
    second call (a reload, a test that builds the stack again), so the guard on
    app.state is what keeps traces honest.
    """
    calls: list[Any] = []
    monkeypatch.setattr(
        "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app",
        lambda app, **kw: calls.append(kw),
    )
    app = _app()

    tel._instrument_fastapi(app)
    tel._instrument_fastapi(app)

    assert len(calls) == 1
    assert calls[0]["excluded_urls"] == "health,ready,live"
    assert app.state._otel_fastapi_instrumented is True


def test_a_missing_fastapi_instrumentor_warns_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.fastapi", None)
    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(tel.logger, "warning", lambda event, **kw: warnings.append({**kw}))

    tel._instrument_fastapi(_app())

    assert warnings == [{"instrumentor": "fastapi"}]


def test_a_failing_fastapi_instrumentor_does_not_stop_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(app: Any, **kw: Any) -> None:
        raise RuntimeError("instrumentation exploded")

    monkeypatch.setattr(
        "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app", boom
    )
    logged: list[str] = []
    monkeypatch.setattr(tel.logger, "exception", lambda event, **kw: logged.append(event))

    tel._instrument_fastapi(_app())  # must not raise

    assert logged == ["otel_instrumentation_failed"]


def test_optional_instrumentors_are_instrumented_and_not_re_instrumented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _FakeInstrumentor()
    already = _FakeInstrumentor()
    already.is_instrumented_by_opentelemetry = True
    monkeypatch.setattr(
        "opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor", lambda: fresh
    )
    monkeypatch.setattr("opentelemetry.instrumentation.redis.RedisInstrumentor", lambda: already)
    for mod in ("opentelemetry.instrumentation.pymongo", "opentelemetry.instrumentation.celery"):
        monkeypatch.setitem(sys.modules, mod, None)

    tel._instrument_clients(tel.DEFAULT_INSTRUMENT)

    assert fresh.instrumented is True
    assert already.instrumented is False


def test_a_failing_optional_instrumentor_is_logged_and_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One broken instrumentor must not take the other three down with it."""

    def explode() -> Any:
        raise RuntimeError("bad instrumentor")

    # httpx comes first in the loop and blows up; redis comes after it and must
    # still be instrumented. pymongo and celery stand in for the ordinary case
    # of a library this service does not install at all.
    survivor = _FakeInstrumentor()
    monkeypatch.setattr("opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor", explode)
    monkeypatch.setattr("opentelemetry.instrumentation.redis.RedisInstrumentor", lambda: survivor)
    for mod in ("opentelemetry.instrumentation.pymongo", "opentelemetry.instrumentation.celery"):
        monkeypatch.setitem(sys.modules, mod, None)

    failures: list[dict[str, Any]] = []
    monkeypatch.setattr(tel.logger, "exception", lambda event, **kw: failures.append({**kw}))

    tel._instrument_clients(tel.DEFAULT_INSTRUMENT)

    assert failures == [{"instrumentor": "HTTPXClientInstrumentor"}]
    assert survivor.instrumented is True


# --- sqlalchemy -----------------------------------------------------------


def test_instrument_sqlalchemy_passes_the_sync_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The instrumentor works on the sync engine behind an async one; handing it
    the async engine instruments nothing and reports no error."""
    instrumentor = _FakeInstrumentor()
    monkeypatch.setattr(
        "opentelemetry.instrumentation.sqlalchemy.SQLAlchemyInstrumentor", lambda: instrumentor
    )
    sync_engine = object()
    engine = SimpleNamespace(sync_engine=sync_engine)

    tel.instrument_sqlalchemy(engine)

    assert instrumentor.kwargs == {"engine": sync_engine}


def test_instrument_sqlalchemy_warns_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.sqlalchemy", None)
    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(tel.logger, "warning", lambda event, **kw: warnings.append({**kw}))

    tel.instrument_sqlalchemy(SimpleNamespace(sync_engine=object()))

    assert warnings == [{"instrumentor": "sqlalchemy"}]


def test_instrument_sqlalchemy_survives_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode() -> Any:
        raise RuntimeError("nope")

    monkeypatch.setattr("opentelemetry.instrumentation.sqlalchemy.SQLAlchemyInstrumentor", explode)
    logged: list[str] = []
    monkeypatch.setattr(tel.logger, "exception", lambda event, **kw: logged.append(event))

    tel.instrument_sqlalchemy(SimpleNamespace(sync_engine=object()))  # must not raise

    assert logged == ["otel_instrumentation_failed"]


# --- FastAPI's built-in OpenTelemetry ------------------------------------------

needs_native = pytest.mark.skipif(
    not tel.fastapi_native_supported(),
    reason="installed FastAPI predates its built-in OpenTelemetry (0.142)",
)


def test_native_exclusions_are_the_ones_metricsmiddleware_uses() -> None:
    """Probes stay out of traces for the same reason they stay out of metrics, and two
    hand-kept lists drift."""
    from py_common.http.middleware.metrics import DEFAULT_EXCLUDED_PATHS

    assert tel._UNTRACED_PATHS == DEFAULT_EXCLUDED_PATHS


@pytest.mark.parametrize(
    ("path", "excluded"),
    [
        ("/health", True),
        ("/health/ready", True),
        ("/live", True),
        ("/ready", True),
        ("/metrics", True),
        ("/healthy", False),  # a prefix match on text would swallow this
        ("/api/health", False),
        ("/items/1", False),
        ("", False),
    ],
)
def test_native_probe_exclusion(path: str, excluded: bool) -> None:
    assert tel.fastapi_native_config()["exclude"]({"path": path}) is excluded


def test_native_config_turns_the_duplicate_metrics_off() -> None:
    assert tel.fastapi_native_config()["metrics"] is False


def test_native_option_skips_the_instrumentor(
    monkeypatch: pytest.MonkeyPatch, stub_sdk: list[Any]
) -> None:
    monkeypatch.setattr(tel, "fastapi_native_supported", lambda: True)
    instrumented: list[Any] = []
    monkeypatch.setattr(tel, "_instrument_fastapi", instrumented.append)

    tel.setup_telemetry(_app(), service_name="svc", fastapi_native=True)

    assert instrumented == []


def test_the_instrumentor_stays_the_default(
    monkeypatch: pytest.MonkeyPatch, stub_sdk: list[Any]
) -> None:
    instrumented: list[Any] = []
    monkeypatch.setattr(tel, "_instrument_fastapi", instrumented.append)

    tel.setup_telemetry(_app(), service_name="svc")

    assert len(instrumented) == 1


def test_an_older_fastapi_falls_back_to_the_instrumentor_and_says_so(
    monkeypatch: pytest.MonkeyPatch, stub_sdk: list[Any]
) -> None:
    """Asking for native on a FastAPI that has none must not mean no tracing."""
    monkeypatch.setattr(tel, "fastapi_native_supported", lambda: False)
    instrumented: list[Any] = []
    monkeypatch.setattr(tel, "_instrument_fastapi", instrumented.append)
    warnings: list[str] = []
    monkeypatch.setattr(tel.logger, "warning", lambda event, **kw: warnings.append(event))

    tel.setup_telemetry(_app(), service_name="svc", fastapi_native=True)

    assert len(instrumented) == 1
    assert warnings == ["fastapi_native_unavailable"]


@needs_native
def test_native_on_an_app_built_for_something_else_warns_about_duplicate_metrics(
    monkeypatch: pytest.MonkeyPatch, stub_sdk: list[Any]
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(tel.logger, "warning", lambda event, **kw: warnings.append(event))

    tel.setup_telemetry(_app(), service_name="svc", fastapi_native=True)  # default FastAPI()

    assert warnings == ["fastapi_native_metrics_enabled"]


@needs_native
def test_create_base_app_configures_native_telemetry_only_when_asked() -> None:
    from py_common.runtime import create_base_app

    native = create_base_app(title="t", version="1", fastapi_native=True)
    plain = create_base_app(title="t", version="1")

    assert native._telemetry["metrics"] is False  # type: ignore[attr-defined]
    assert plain._telemetry["metrics"] is True  # type: ignore[attr-defined]


@needs_native
def test_a_telemetry_config_passed_by_the_caller_wins() -> None:
    from py_common.runtime import create_base_app

    mine = {"metrics": True}
    app = create_base_app(title="t", version="1", fastapi_native=True, telemetry=mine)

    assert app._telemetry["metrics"] is True  # type: ignore[attr-defined]


@needs_native
def test_the_native_path_end_to_end_with_a_real_sdk() -> None:
    """Traces from the built-in telemetry, probes left out, and none of the metrics
    that would duplicate MetricsMiddleware -- through the real FastAPI and the real SDK."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    spans = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    reader = InMemoryMetricReader()
    config = {
        **tel.fastapi_native_config(),
        "tracer_provider": tracer_provider,
        "meter_provider": MeterProvider(metric_readers=[reader]),
    }
    app = FastAPI(telemetry=config)  # type: ignore[arg-type]

    @app.get("/items/{n}")
    def item(n: int) -> dict[str, int]:
        return {"n": n}

    @app.get("/health/ready")
    def ready() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    client.get("/items/1")
    client.get("/health/ready")

    servers = [s.name for s in spans.get_finished_spans() if s.kind.name == "SERVER"]
    assert servers == ["GET /items/{n}"]
    data = reader.get_metrics_data()
    names = (
        [m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics]
        if data
        else []
    )
    assert names == []
