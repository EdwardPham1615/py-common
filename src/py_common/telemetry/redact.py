"""Keep credentials out of exported spans.

Instrumentors record what they are given, and what they are given is often a secret:
the httpx instrumentor writes the full URL, query string included, as ``http.url``;
the ASGI/FastAPI instrumentor does the same for the request; FastAPI's built-in
telemetry writes ``url.query``; a presigned S3 URL is a bearer credential in the
shape of a link. A span then travels to a collector, a vendor and a retention policy
that nobody chose for that secret.

:class:`RedactingSpanExporter` wraps the real exporter and rewrites those values on
the way out. It is an exporter and not a span processor because that is the only
place a rewrite reliably sticks:

* At ``on_start`` attributes can be changed, but the ASGI instrumentor creates the span
  with its URL attributes and then re-applies the same attributes with
  ``set_attribute``, overwriting whatever a processor wrote in between. httpx does
  not, which is how this was first mistaken for working. (Pinned by a test.)
* At ``on_end`` it depends on the SDK: on opentelemetry-sdk 1.45 the attributes are
  immutable and assigning raises ``TypeError`` -- the original value is exported
  anyway -- while on 1.42, the lowest this library allows, they are still writable.
  A rewrite that only works on some of the supported SDKs is not one to build on.

Events (an exception's message, say) are not scrubbed: they carry free text rather
than a URL, and guessing at free text would produce false confidence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from opentelemetry.attributes import BoundedAttributes
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

REDACTED = "REDACTED"

# Query parameters whose value is a credential or something that mints one. Matched
# by exact name, case-insensitively: `key` must not eat `monkey`.
DEFAULT_REDACTED_QUERY_PARAMS: tuple[str, ...] = (
    "key",
    "token",
    "code",
    "state",
    "password",
    "signature",
    "X-Amz-Signature",
    "X-Amz-Credential",
    "X-Amz-Security-Token",
)

# Attributes that carry a URL or the query part of one.
_URL_ATTRIBUTES = frozenset({"url.full", "http.url", "url.query", "http.target"})

# `http.request.header.<name>` / `http.response.header.<name>`, which instrumentors
# fill only when asked to capture headers, with the name lower-cased and dashes as
# underscores. These are the ones that are credentials by definition.
_HEADER_PREFIXES = ("http.request.header.", "http.response.header.")
_SECRET_HEADERS = frozenset({"authorization", "proxy_authorization", "cookie", "set_cookie"})


class RedactingSpanExporter(SpanExporter):
    """Replace secret query values and credential headers with ``REDACTED``.

    ``query_params`` adds names to :data:`DEFAULT_REDACTED_QUERY_PARAMS`; it never
    removes any. Spans with nothing to scrub are passed through as they are.
    """

    def __init__(self, inner: SpanExporter, query_params: Iterable[str] = ()) -> None:
        self._inner = inner
        names = {n.lower() for n in (*DEFAULT_REDACTED_QUERY_PARAMS, *query_params)}
        alternation = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        # The separator is a group so it survives the substitution; `^` covers a bare
        # `url.query`, which has no leading `?`.
        self._pattern = re.compile(
            rf"(?P<sep>^|[?&;])(?P<name>{alternation})=[^&#;]*", re.IGNORECASE
        )

    def redact_url(self, value: str) -> str:
        return self._pattern.sub(rf"\g<sep>\g<name>={REDACTED}", value)

    def _redacted_attributes(self, attributes: Mapping[str, Any]) -> dict[str, Any] | None:
        """The scrubbed copy, or ``None`` when nothing needed scrubbing."""
        changed: dict[str, Any] = {}
        for key, value in attributes.items():
            if key in _URL_ATTRIBUTES and isinstance(value, str):
                redacted = self.redact_url(value)
                if redacted != value:
                    changed[key] = redacted
            elif key.startswith(_HEADER_PREFIXES) and key.rsplit(".", 1)[-1] in _SECRET_HEADERS:
                changed[key] = _redacted_like(value)
        return {**attributes, **changed} if changed else None

    def _redacted_span(self, span: ReadableSpan) -> ReadableSpan:
        scrubbed = self._redacted_attributes(span.attributes or {})
        if scrubbed is None:
            return span
        return ReadableSpan(
            name=span.name,
            context=span.get_span_context(),
            parent=span.parent,
            resource=span.resource,
            attributes=BoundedAttributes(attributes=scrubbed, immutable=True),
            events=span.events,
            links=span.links,
            kind=span.kind,
            instrumentation_info=span.instrumentation_info,
            status=span.status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export([self._redacted_span(span) for span in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)


def _redacted_like(value: Any) -> Any:
    """Header attributes are string sequences; keep the shape, drop the content."""
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [REDACTED] * len(value)
    return REDACTED
