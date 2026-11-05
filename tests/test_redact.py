"""RedactingSpanExporter: what leaves the process in a span.

Layers, deliberately. The pure URL rewrite is table-driven because the failure modes
are string handling. The exporter is then run on a real ``TracerProvider`` with an
in-memory exporter behind it, because *where* it sits is an SDK fact (attributes are
read-only by ``on_end`` on current SDKs, and the ASGI instrumentor overwrites what a
processor wrote at ``on_start``) that only the real SDK and the real instrumentor can confirm.
"""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from py_common.telemetry.redact import (
    DEFAULT_REDACTED_QUERY_PARAMS,
    REDACTED,
    RedactingSpanExporter,
)


def _redactor(*query_params: str) -> RedactingSpanExporter:
    return RedactingSpanExporter(InMemorySpanExporter(), query_params)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://h/p?key=S3CRET&q=1", f"http://h/p?key={REDACTED}&q=1"),
        ("http://h/p?q=1&token=abc", f"http://h/p?q=1&token={REDACTED}"),
        # A bare query string, as url.query carries it: no leading "?".
        ("code=1&state=2&page=3", f"code={REDACTED}&state={REDACTED}&page=3"),
        # Case-insensitive on the name; the name's own spelling is kept.
        (
            "http://h/?X-Amz-Signature=deadbeef&X-AMZ-Credential=AKIA%2Fx",
            f"http://h/?X-Amz-Signature={REDACTED}&X-AMZ-Credential={REDACTED}",
        ),
        (
            "http://h/?X-Amz-Security-Token=tok&password=hunter2",
            f"http://h/?X-Amz-Security-Token={REDACTED}&password={REDACTED}",
        ),
        # Exact names only: `key` must not eat `monkey`, nor `token` eat `tokens`.
        ("http://h/?monkey=1&tokens=2&keyboard=3", "http://h/?monkey=1&tokens=2&keyboard=3"),
        # A value with the name in it is not a parameter.
        ("http://h/path/key=1", "http://h/path/key=1"),
        # The fragment is not part of the query and is left alone.
        ("http://h/?key=abc#frag", f"http://h/?key={REDACTED}#frag"),
        ("http://h/no-query", "http://h/no-query"),
        ("", ""),
    ],
)
def test_url_rewrite(url: str, expected: str) -> None:
    assert _redactor().redact_url(url) == expected


def test_extra_names_are_added_to_the_defaults_not_swapped_for_them() -> None:
    processor = _redactor("session_id", "API-Key")

    redacted = processor.redact_url("http://h/?session_id=1&api-key=2&token=3&q=4")

    assert redacted == f"http://h/?session_id={REDACTED}&api-key={REDACTED}&token={REDACTED}&q=4"


def test_every_default_name_is_actually_redacted() -> None:
    """Guards the list against a name being dropped from the regex while still
    sitting in the tuple, which is how a default quietly stops working."""
    processor = _redactor()
    for name in DEFAULT_REDACTED_QUERY_PARAMS:
        assert processor.redact_url(f"http://h/?{name}=v") == f"http://h/?{name}={REDACTED}"


@pytest.fixture
def traced() -> Any:
    behind = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(behind)))
    return provider, behind


def _exported(traced: Any, attributes: dict[str, Any]) -> dict[str, Any]:
    provider, behind = traced
    with provider.get_tracer("t").start_as_current_span("s", attributes=attributes):
        pass
    (span,) = behind.get_finished_spans()
    return dict(span.attributes)


@pytest.mark.parametrize("attribute", ["http.url", "url.full", "url.query", "http.target"])
def test_each_url_attribute_is_scrubbed_in_the_exported_span(traced: Any, attribute: str) -> None:
    exported = _exported(traced, {attribute: "/p?key=S3CRET&page=2"})

    assert exported[attribute] == f"/p?key={REDACTED}&page=2"


def test_credential_headers_are_blanked_and_others_kept(traced: Any) -> None:
    exported = _exported(
        traced,
        {
            "http.request.header.authorization": ["Bearer eyJ"],
            "http.request.header.cookie": ["sid=abc", "x=y"],
            "http.response.header.set_cookie": ["sid=new"],
            "http.request.header.content_type": ["application/json"],
        },
    )

    assert exported["http.request.header.authorization"] == (REDACTED,)
    assert exported["http.request.header.cookie"] == (REDACTED, REDACTED)  # shape kept
    assert exported["http.response.header.set_cookie"] == (REDACTED,)
    assert exported["http.request.header.content_type"] == ("application/json",)


def test_unrelated_attributes_and_clean_urls_are_untouched(traced: Any) -> None:
    exported = _exported(traced, {"http.url": "http://h/ok?page=1", "user.id": "u1", "n": 3})

    assert exported == {"http.url": "http://h/ok?page=1", "user.id": "u1", "n": 3}


def test_a_span_without_attributes_is_fine(traced: Any) -> None:
    assert _exported(traced, {}) == {}


def test_a_late_set_attribute_is_scrubbed_too(traced: Any) -> None:
    """The case an ``on_start`` processor cannot catch: the value is written after
    the span is open. Redacting on the way out sees it whenever it arrived."""
    provider, behind = traced
    with provider.get_tracer("t").start_as_current_span("s") as span:
        span.set_attribute("http.url", "http://h/?token=LATE")

    assert behind.get_finished_spans()[0].attributes["http.url"] == f"http://h/?token={REDACTED}"


def test_the_rest_of_the_span_survives_the_rewrite(traced: Any) -> None:
    provider, behind = traced
    tracer = provider.get_tracer("t", "1.2")
    with (
        tracer.start_as_current_span("outer"),
        tracer.start_as_current_span("inner", attributes={"http.url": "?key=S"}) as inner,
    ):
        inner.add_event("tick", {"n": 1})
        inner.set_status(trace_status_error())

    inner_span = next(s for s in behind.get_finished_spans() if s.name == "inner")
    outer_span = next(s for s in behind.get_finished_spans() if s.name == "outer")
    assert inner_span.parent is not None
    assert inner_span.parent.span_id == outer_span.context.span_id
    assert inner_span.context.trace_id == outer_span.context.trace_id
    assert [e.name for e in inner_span.events] == ["tick"]
    assert inner_span.status.status_code.name == "ERROR"
    assert inner_span.instrumentation_scope.name == "t"
    assert inner_span.resource is not None
    assert inner_span.end_time is not None and inner_span.start_time is not None


def trace_status_error() -> Any:
    from opentelemetry.trace import Status, StatusCode

    return Status(StatusCode.ERROR)


def test_the_asgi_instrumentor_cannot_undo_it() -> None:
    """The regression behind the exporter design. FastAPIInstrumentor creates the
    server span with its URL attributes and then re-applies them with set_attribute,
    which overwrites anything an on_start processor wrote: this request's token
    reached the collector in clear until the rewrite moved to the exporter."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    behind = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(behind)))
    app = FastAPI()

    @app.get("/cb")
    def callback() -> dict[str, bool]:
        return {"ok": True}

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    try:
        TestClient(app).get("/cb?token=S3CRET&page=2")
    finally:
        FastAPIInstrumentor.uninstrument_app(app)

    urls = [
        s.attributes["http.url"]
        for s in behind.get_finished_spans()
        if s.attributes and "http.url" in s.attributes
    ]
    assert urls == [f"http://testserver/cb?token={REDACTED}&page=2"]
