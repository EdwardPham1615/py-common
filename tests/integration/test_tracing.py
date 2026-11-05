"""What actually reaches the collector: secrets must not, and Redis must not leak args.

Same shape as ``test_telemetry_integration``: each scenario runs in a subprocess
(the tracer provider is process-global) and the assertion reads Jaeger back, because
a redaction that happens in the process proves nothing about the bytes that left it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import pytest

from .test_telemetry_integration import _wait_for_traces

pytestmark = pytest.mark.integration

SECRET = "SUPERSECRET-9f3a"

HTTPX_SCRIPT = """
import sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from py_common.telemetry import setup_tracing, shutdown_tracing

service_name, endpoint, secret = sys.argv[1:4]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
setup_tracing(service_name=service_name, otlp_endpoint=endpoint, redact_query_params=["sid"])

base = f"http://127.0.0.1:{server.server_address[1]}"
httpx.get(f"{base}/files?key={secret}&page=2&sid={secret}")
shutdown_tracing()
server.shutdown()
"""

FASTAPI_SCRIPT = """
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

from py_common.telemetry import setup_telemetry, shutdown_telemetry

service_name, endpoint, secret = sys.argv[1:4]
app = FastAPI()


@app.get("/cb")
async def callback() -> dict:
    return {"ok": True}


setup_telemetry(app, service_name=service_name, otlp_endpoint=endpoint, metrics_enabled=False)
TestClient(app).get(f"/cb?code={secret}&state={secret}&page=2")
shutdown_telemetry()
"""


def _run(script: str, service_name: str, endpoint: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, this interpreter, literal script
        [sys.executable, "-c", script, service_name, endpoint, SECRET],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def _tag_values(traces: list[dict], key: str) -> list[str]:
    return [
        str(tag["value"])
        for trace in traces
        for span in trace["spans"]
        for tag in span.get("tags", [])
        if tag["key"] == key
    ]


def test_httpx_urls_reach_the_collector_without_their_secrets(
    otlp_endpoint: str, jaeger_query_url: str
) -> None:
    """The httpx instrumentor records the whole URL, query string included. A key in
    a query is the commonest way a secret ends up in a URL, and then in a span."""
    service_name = f"py_common-it-{uuid.uuid4().hex[:8]}"
    _run(HTTPX_SCRIPT, service_name, otlp_endpoint)
    traces = _wait_for_traces(jaeger_query_url, service_name)

    assert traces, f"no traces arrived for {service_name}"
    (url,) = _tag_values(traces, "http.url")
    assert url.endswith("/files?key=REDACTED&page=2&sid=REDACTED")  # sid: configured extra
    assert SECRET not in json.dumps(traces)


def test_fastapi_requests_reach_the_collector_without_their_secrets(
    otlp_endpoint: str, jaeger_query_url: str
) -> None:
    service_name = f"py_common-it-{uuid.uuid4().hex[:8]}"
    _run(FASTAPI_SCRIPT, service_name, otlp_endpoint)
    traces = _wait_for_traces(jaeger_query_url, service_name)

    assert traces, f"no traces arrived for {service_name}"
    urls = _tag_values(traces, "http.url")
    assert urls
    assert all("code=REDACTED&state=REDACTED&page=2" in u for u in urls), urls
    assert SECRET not in json.dumps(traces)


def test_redis_spans_do_not_carry_command_arguments(redis_url: str) -> None:
    """Pins the *instrumentor's* default, not code of ours.

    The plan assumed ``RedisInstrumentor(sanitize_query=True)``; no such parameter
    exists in 0.66b0-b2, and none is needed, because ``db.statement`` already reads
    ``SET ? ? ? ?``. This test exists so a release that stops sanitising turns red
    here instead of putting session keys and cached values into every trace.
    """
    import redis
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = RedisInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        client = redis.Redis.from_url(redis_url)
        key = f"session:{SECRET}"
        client.set(key, SECRET, ex=30)
        client.get(key)
        client.delete(key)
        client.close()
    finally:
        instrumentor.uninstrument()

    spans = exporter.get_finished_spans()
    assert {s.name for s in spans} >= {"SET", "GET", "DEL"}, [s.name for s in spans]
    statements = [s.attributes.get("db.statement") for s in spans if s.attributes]
    assert "SET ? ? ? ?" in statements
    assert SECRET not in json.dumps([dict(s.attributes or {}) for s in spans])
    assert os.getenv("REDIS_TEST_URL")  # the fixture skipped otherwise; belt and braces


S3_SCRIPT = """
import asyncio, sys

from py_common.config import StorageSettings
from py_common.storage import ObjectStorageClient
from py_common.telemetry import setup_tracing, shutdown_tracing

service_name, tracing_endpoint, secret, s3_endpoint = sys.argv[1:5]
setup_tracing(service_name=service_name, otlp_endpoint=tracing_endpoint)


async def main() -> None:
    settings = StorageSettings(
        endpoint_url=s3_endpoint, access_key="pycommon", secret_key="pycommon123",
        bucket="tracing-it", use_path_style=True,
    )
    async with ObjectStorageClient(settings) as storage:
        await storage.ensure_bucket()
        await storage.put_object(f"users/{secret}.txt", b"hello", content_type="text/plain")
        await storage.head_object(f"users/{secret}.txt")
        await storage.get_object_bytes(f"users/{secret}.txt")
        key = f"users/{secret}.txt"
        await storage.get_presigned_url(key, method="put_object", content_length=5)
        _ = [page async for page in storage.list_objects("users/")]
        await storage.delete_object(f"users/{secret}.txt")


asyncio.run(main())
shutdown_tracing()
"""


def test_s3_calls_are_traced_and_the_trace_holds_no_signature_or_key(
    otlp_endpoint: str, jaeger_query_url: str, storage_settings: object
) -> None:
    """aiobotocore has no instrumentor, so these spans exist only if the client makes
    them. Read back from the collector: operation names, the bucket, and -- the point
    of the second half -- no signed URL and no object key anywhere in the trace."""
    service_name = f"py_common-it-{uuid.uuid4().hex[:8]}"
    result = subprocess.run(  # noqa: S603 - fixed argv, this interpreter, literal script
        [
            sys.executable,
            "-c",
            S3_SCRIPT,
            service_name,
            otlp_endpoint,
            SECRET,
            storage_settings.endpoint_url,  # type: ignore[attr-defined]
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    traces = _wait_for_traces(jaeger_query_url, service_name)

    names = {span["operationName"] for trace in traces for span in trace["spans"]}
    assert {
        "s3 PutObject",
        "s3 HeadObject",
        "s3 GetObject",
        "s3 GeneratePresignedUrl",
        "s3 ListObjectsV2",
        "s3 DeleteObject",
    } <= names, names
    assert "tracing-it" in _tag_values(traces, "aws.s3.bucket")
    everything = json.dumps(traces)
    assert SECRET not in everything  # the object key
    assert "X-Amz-Signature" not in everything


NATIVE_SCRIPT = """
import sys

from fastapi.testclient import TestClient

from py_common.runtime import create_base_app
from py_common.telemetry import fastapi_native_supported, setup_telemetry, shutdown_telemetry

service_name, endpoint, secret = sys.argv[1:4]
assert fastapi_native_supported(), "this FastAPI has no built-in OpenTelemetry"

app = create_base_app(title="t", version="1", fastapi_native=True)


@app.get("/cb")
async def callback() -> dict:
    return {"ok": True}


@app.get("/health/ready")
async def ready() -> dict:
    return {"ok": True}


setup_telemetry(
    app, service_name=service_name, otlp_endpoint=endpoint, metrics_enabled=False,
    fastapi_native=True,
)
client = TestClient(app)
client.get(f"/cb?code={secret}&page=2")
client.get("/health/ready")
shutdown_telemetry()
"""


def test_fastapi_native_telemetry_reaches_the_collector_redacted_and_without_probes(
    otlp_endpoint: str, jaeger_query_url: str
) -> None:
    """FastAPI's built-in telemetry through the whole stack: the provider setup_telemetry
    installs, the exporter wrapper, and the app built by create_base_app.

    The native path records the query as ``url.query`` rather than ``http.url``, so this
    is also the only place that attribute is checked leaving the process."""
    from py_common.telemetry import fastapi_native_supported

    if not fastapi_native_supported():
        pytest.skip("installed FastAPI predates its built-in OpenTelemetry (0.142)")
    service_name = f"py_common-it-{uuid.uuid4().hex[:8]}"
    _run(NATIVE_SCRIPT, service_name, otlp_endpoint)
    traces = _wait_for_traces(jaeger_query_url, service_name)

    assert traces, f"no traces arrived for {service_name}"
    operations = {span["operationName"] for trace in traces for span in trace["spans"]}
    assert "GET /cb" in operations, operations
    assert not any("health" in op for op in operations), operations  # probes excluded
    assert _tag_values(traces, "url.query") == ["code=REDACTED&page=2"]
    assert SECRET not in json.dumps(traces)
