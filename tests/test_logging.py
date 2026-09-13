"""setup_logging: what actually reaches stdout, and in what shape.

These assert on the rendered line rather than on the processor list. A pipeline
that is configured correctly and still emits something a log collector cannot
parse is the failure worth catching, and only the output shows it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from pycommon.logging import current_request_id, get_logger, setup_logging


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """Logging config is process-global; put it back exactly as it was."""
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    quieted = {name: logging.getLogger(name).level for name in ("uvicorn.access", "httpx")}
    try:
        yield
    finally:
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()
        root.handlers[:] = handlers
        root.setLevel(level)
        for name, lvl in quieted.items():
            logging.getLogger(name).setLevel(lvl)


def _emit(capsys: pytest.CaptureFixture[str], name: str, **setup: Any) -> list[dict[str, Any]]:
    """Configure logging, emit one event, and return the parsed JSON lines."""
    setup_logging(**setup)
    get_logger(name).info("hello", order_id="o-1")
    out = capsys.readouterr().out.strip()
    return [json.loads(line) for line in out.splitlines() if line]


def test_json_output_is_parseable_ecs(capsys: pytest.CaptureFixture[str]) -> None:
    """A log collector has to be able to read this; that is the whole point."""
    (record,) = _emit(capsys, "t.ecs", service_name="orders", environment="staging")

    assert record["message"] == "hello"
    # ecs_logging emits the ECS names as flat dotted keys, which is what an ECS
    # agent reads; asserting on those is asserting on the contract.
    assert record["log.level"] == "info"
    assert record["logger"] == "t.ecs"
    assert record["service"] == {"name": "orders", "environment": "staging"}
    assert record["order_id"] == "o-1"
    # ECS requires these two, and an agent that validates the schema drops lines
    # without them rather than indexing them.
    assert record["ecs.version"]
    assert record["@timestamp"].endswith("Z")


def test_console_renderer_is_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(json_logs=False)
    get_logger("t.console").info("hello")
    out = capsys.readouterr().out

    assert "hello" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip())


def test_level_filters_events(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(level="WARNING")
    logger = get_logger("t.level")
    logger.info("dropped")
    logger.warning("kept")

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [record["message"] for record in lines] == ["kept"]


def test_unknown_level_falls_back_to_info(capsys: pytest.CaptureFixture[str]) -> None:
    """A typo in LOG_LEVEL must not silence the service."""
    (record,) = _emit(capsys, "t.badlevel", level="WARN_ING_TYPO")

    assert record["message"] == "hello"
    assert logging.getLogger().level == logging.INFO


def test_lowercase_level_is_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(level="debug")
    assert logging.getLogger().level == logging.DEBUG


def test_context_variables_reach_the_line(capsys: pytest.CaptureFixture[str]) -> None:
    """The correlation the rest of the library depends on.

    Middleware and the gRPC interceptors bind ``request_id`` into contextvars
    and never pass it to a log call; if merge_contextvars were dropped from the
    chain, every one of those bindings would silently stop appearing.
    """
    structlog.contextvars.bind_contextvars(request_id="rid-42", tenant="acme")
    (record,) = _emit(capsys, "t.ctx")

    assert record["request_id"] == "rid-42"
    assert record["tenant"] == "acme"


def test_trace_ids_are_added_inside_a_span(capsys: pytest.CaptureFixture[str]) -> None:
    """Logs must join up with traces; that is what makes a trace ID worth having."""
    provider = TracerProvider()
    tracer = provider.get_tracer("test")

    setup_logging()
    with tracer.start_as_current_span("unit") as span:
        get_logger("t.trace").info("in-span")
        ctx = span.get_span_context()

    (record,) = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert record["trace"]["id"] == format(ctx.trace_id, "032x")
    assert record["span"]["id"] == format(ctx.span_id, "016x")


def test_no_trace_fields_without_a_span(capsys: pytest.CaptureFixture[str]) -> None:
    """An invalid span context must not produce a line claiming a zeroed trace."""
    assert not trace.get_current_span().get_span_context().is_valid

    (record,) = _emit(capsys, "t.notrace")
    assert "trace" not in record
    assert "span" not in record


def test_stdlib_logging_is_rendered_the_same_way(capsys: pytest.CaptureFixture[str]) -> None:
    """Third-party libraries log through stdlib, not structlog.

    ``foreign_pre_chain`` is what puts those lines in the same ECS shape. Without
    it half the output of a running service -- SQLAlchemy, uvicorn, anything
    vendored -- arrives as unparseable plain text.
    """
    setup_logging(service_name="orders", environment="prod")
    logging.getLogger("some.vendor.lib").warning("from stdlib")

    (record,) = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert record["message"] == "from stdlib"
    assert record["log.level"] == "warning"
    assert record["logger"] == "some.vendor.lib"
    assert record["service"] == {"name": "orders", "environment": "prod"}
    assert record["ecs.version"]


def test_noisy_libraries_are_quieted() -> None:
    setup_logging(level="DEBUG")

    for name in ("uvicorn.access", "httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING


def test_reconfiguring_does_not_duplicate_output(capsys: pytest.CaptureFixture[str]) -> None:
    """Handlers are replaced, not stacked.

    Calling this twice happens (a test suite, a reload); leaving both handlers
    attached would double every line the service logs from then on.
    """
    setup_logging()
    setup_logging()
    get_logger("t.twice").info("once")

    assert len(logging.getLogger().handlers) == 1
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_current_request_id_reads_the_bound_value() -> None:
    assert current_request_id() is None

    structlog.contextvars.bind_contextvars(request_id="rid-7")
    assert current_request_id() == "rid-7"


def test_current_request_id_stringifies_a_non_string() -> None:
    """It is the one definition of the ID, so it always answers with a string."""
    structlog.contextvars.bind_contextvars(request_id=12345)
    assert current_request_id() == "12345"
