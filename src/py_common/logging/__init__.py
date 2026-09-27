"""ECS-structured logging with OpenTelemetry correlation."""

from __future__ import annotations

import logging
import sys
from typing import Any

import ecs_logging
import structlog
from opentelemetry import trace


def _add_otel_context(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Inject OpenTelemetry trace/span IDs into every log event (ECS tracing fields)."""
    span = trace.get_current_span()
    ctx = span.get_span_context() if span else None
    if ctx and ctx.is_valid:
        event_dict["trace"] = {
            "id": format(ctx.trace_id, "032x"),
        }
        event_dict["span"] = {
            "id": format(ctx.span_id, "016x"),
        }
    return event_dict


def _add_service_info(
    service_name: str,
    environment: str,
) -> Any:
    def processor(
        logger: Any,
        method_name: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        event_dict.setdefault(
            "service",
            {"name": service_name, "environment": environment},
        )
        return event_dict

    return processor


def setup_logging(
    *,
    level: str = "INFO",
    service_name: str = "app",
    environment: str = "dev",
    json_logs: bool = True,
) -> None:
    """Configure structlog + stdlib logging for ECS JSON output."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    # The level and timestamp processors differ per renderer, because the two
    # renderers want opposite things.
    #
    # ecs_logging derives ``log.level`` from the method name itself and only
    # fills in ``@timestamp`` when the event does not already carry one. Adding
    # ``add_log_level`` and a plain ``timestamp`` key on top of that produced a
    # second copy of each -- ``level`` beside ``log.level``, ``timestamp``
    # beside ``@timestamp`` -- on every line every service ever wrote. Stamping
    # straight into ``@timestamp`` keeps the time the event happened rather than
    # the time it was rendered, which is what ecs_logging would otherwise use.
    #
    # ConsoleRenderer is the opposite: it reads ``level`` and ``timestamp`` to
    # build its prefix, and without them a dev run loses the level entirely and
    # trails ``@timestamp`` as an ordinary key=value pair.
    renderer: Any
    if json_logs:
        level_processors: list[Any] = []
        timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True, key="@timestamp")
        renderer = ecs_logging.StructlogFormatter()
    else:
        level_processors = [structlog.stdlib.add_log_level]
        timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
        renderer = structlog.dev.ConsoleRenderer()

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        *level_processors,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        _add_otel_context,
        _add_service_info(service_name, environment),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # Adopt uvicorn's own loggers, so the format holds however the process was
    # started. The `uvicorn` CLI applies uvicorn's LOGGING_CONFIG via dictConfig
    # *before importing the app*, which gives the `uvicorn` logger its own handler
    # and `propagate: False`. Replacing the root handler above therefore never
    # reaches it, and uvicorn's startup and shutdown lines stay plain text in the
    # middle of an otherwise ECS stream -- a shipper parsing one JSON object per
    # line drops exactly the lines you want during an incident. `run_uvicorn`
    # avoids this by passing `log_config=None`, but nothing stops a consumer from
    # using the CLI.
    #
    # `uvicorn.error` needs no entry of its own: uvicorn gives it no handler, so it
    # propagates to `uvicorn` and is carried by the same fix. Listed anyway, so a
    # future change to that config cannot quietly reintroduce the problem.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # Quiet noisy libs.
    #
    # `uvicorn.access` is deliberately *not* adopted above. RequestContextMiddleware
    # already emits the access log (`request_completed`, with status, duration,
    # route and request ID), so letting uvicorn's version through as well would put
    # two access lines on every request. Silencing it by level is what keeps the
    # library's own access log the only one -- do not "finish the job" by adding
    # uvicorn.access to the loop above.
    for name in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


REQUEST_ID_KEY = "request_id"


def current_request_id() -> str | None:
    """Return the request ID bound to the current context, if any.

    The one place that reads it. ``RequestContextMiddleware`` binds it here and
    on the ASGI scope, and the gRPC interceptors bind it here only, so this is
    the definition that works everywhere — HTTP handlers, outbound clients,
    gRPC servicers, Celery tasks. Correlation is worth nothing if two parts of
    a response disagree about the ID, which is what several independent readers
    eventually produce.
    """
    value = structlog.contextvars.get_contextvars().get(REQUEST_ID_KEY)
    return str(value) if value is not None else None
