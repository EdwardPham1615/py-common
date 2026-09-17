"""setup_telemetry against a real OTLP collector.

The existing telemetry tests assert control flow with the SDK mocked out, which
proves the branches are right and nothing about whether spans leave the process.
Exporter configuration is exactly the kind of thing that looks correct and
silently drops everything: a wrong endpoint, a TLS mismatch, a provider that was
never flushed. The only way to know is to ask the collector what it received.

Each scenario runs in a **subprocess**. ``setup_telemetry`` installs a
process-global TracerProvider once and later calls reuse it, so running it inside
the test session would either contaminate every other test or silently reuse a
provider some earlier test had installed — and then pass without exporting
anything.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.integration


EXPORT_SCRIPT = """
import sys
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace

from py_common.config import BaseAppSettings
from py_common.http.middleware import apply_standard_middleware
from py_common.telemetry import setup_telemetry, shutdown_telemetry

service_name, endpoint = sys.argv[1], sys.argv[2]

app = FastAPI()

@app.get("/work")
async def work() -> dict:
    return {"ok": True}

apply_standard_middleware(app, BaseAppSettings(_env_file=None))
setup_telemetry(
    app,
    service_name=service_name,
    otlp_endpoint=endpoint,
    insecure=True,
    metrics_enabled=False,
)

# A span created by hand, and one produced by serving a real request.
tracer = trace.get_tracer("integration")
with tracer.start_as_current_span("manual-span") as span:
    span.set_attribute("test.kind", "manual")

resp = TestClient(app).get("/work", headers={"X-Request-ID": "req-from-test"})
assert resp.status_code == 200

shutdown_telemetry()
"""


def _export(service_name: str, endpoint: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, this interpreter, literal script
        [sys.executable, "-c", EXPORT_SCRIPT, service_name, endpoint],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(f"exporter subprocess failed:\n{result.stdout}\n{result.stderr}")


def _wait_for_traces(query_url: str, service_name: str, *, timeout: float = 30.0) -> list[dict]:
    """Poll until the collector has the trace, or give up.

    Polling rather than a fixed sleep: the batch processor flushes on shutdown
    and Jaeger indexes asynchronously, so the delay is real but unpredictable,
    and a sleep long enough to be safe makes every run slow.
    """
    deadline = time.monotonic() + timeout
    last: list[dict] = []
    while time.monotonic() < deadline:
        resp = httpx.get(f"{query_url}/api/traces", params={"service": service_name}, timeout=10)
        if resp.status_code == 200:
            last = resp.json().get("data") or []
            if last:
                return last
        time.sleep(0.5)
    return last


def test_spans_configured_by_setup_telemetry_reach_the_collector(
    otlp_endpoint: str, jaeger_query_url: str
) -> None:
    """The claim the README makes and nothing verified: configure the endpoint,
    and traces show up there."""
    service_name = f"py_common-it-{uuid.uuid4().hex[:8]}"

    _export(service_name, otlp_endpoint)
    traces = _wait_for_traces(jaeger_query_url, service_name)

    assert traces, f"no traces arrived for {service_name}"
    names = {span["operationName"] for trace in traces for span in trace["spans"]}
    assert "manual-span" in names


def test_the_service_registers_itself_with_the_collector(
    otlp_endpoint: str, jaeger_query_url: str
) -> None:
    """service.name comes from the Resource setup_telemetry builds. Getting it
    wrong is invisible locally and makes a service unfindable in the UI."""
    service_name = f"py_common-it-{uuid.uuid4().hex[:8]}"

    _export(service_name, otlp_endpoint)
    _wait_for_traces(jaeger_query_url, service_name)

    services = httpx.get(f"{jaeger_query_url}/api/services", timeout=10).json().get("data") or []
    assert service_name in services


def test_served_requests_carry_the_request_id_on_their_span(
    otlp_endpoint: str, jaeger_query_url: str
) -> None:
    """The correlation story in the README: X-Request-ID is set as the span
    attribute http.request.id, so a log line and a trace can be joined. It is
    asserted here against what the collector actually stored."""
    service_name = f"py_common-it-{uuid.uuid4().hex[:8]}"

    _export(service_name, otlp_endpoint)
    traces = _wait_for_traces(jaeger_query_url, service_name)

    tags = [
        (tag["key"], tag.get("value"))
        for trace in traces
        for span in trace["spans"]
        for tag in span.get("tags", [])
    ]
    assert ("http.request.id", "req-from-test") in tags, json.dumps(tags[:20], indent=2)
