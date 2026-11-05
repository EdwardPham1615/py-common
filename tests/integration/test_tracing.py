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
