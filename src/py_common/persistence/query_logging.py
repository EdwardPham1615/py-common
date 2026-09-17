"""Structured SQL query logging via SQLAlchemy event listeners."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from py_common.logging import get_logger

_QUERY_START_ATTR = "_py_common_query_start"
_INSTALLED_KEY = "_py_common_query_logger"
_MAX_STATEMENT_CHARS = 2000

logger = get_logger(__name__)


def _truncate(statement: str) -> str:
    if len(statement) <= _MAX_STATEMENT_CHARS:
        return statement
    return statement[:_MAX_STATEMENT_CHARS] + "…"


def _elapsed_ms(context: Any) -> float | None:
    started: float | None = (
        getattr(context, _QUERY_START_ATTR, None) if context is not None else None
    )
    if started is None:
        return None
    return round((time.perf_counter() - started) * 1000, 2)


def install_query_logger(
    engine: AsyncEngine,
    *,
    slow_query_threshold_ms: float = 0.0,
    log_params: bool = False,
    log: Any | None = None,
) -> AsyncEngine:
    """Attach listeners that emit structured query logs.

    - ``slow_query_threshold_ms == 0``: log every successful query at ``debug``.
    - ``slow_query_threshold_ms > 0``: log only queries at/above the threshold at ``warning``.

    Failed queries are always logged at ``warning`` regardless of the threshold —
    a deadlock, statement timeout or constraint violation is worth seeing however
    fast it failed, and those are the queries you actually need when debugging.

    Timing is stored on the per-execution ``context`` rather than on the
    connection: a failed query never reaches ``after_cursor_execute``, so a
    connection-scoped stack would grow for the whole life of the pooled
    connection and mis-pair later measurements.

    Idempotent: subsequent calls on the same engine are no-ops.
    """
    sync_engine = engine.sync_engine
    if getattr(sync_engine, _INSTALLED_KEY, False):
        return engine

    bound_logger = log or logger

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _before_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if context is not None:
            setattr(context, _QUERY_START_ATTR, time.perf_counter())

    @event.listens_for(sync_engine, "after_cursor_execute")
    def _after_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        duration_ms = _elapsed_ms(context)
        if duration_ms is None:
            return

        if slow_query_threshold_ms > 0 and duration_ms < slow_query_threshold_ms:
            return

        payload: dict[str, Any] = {
            "db": {
                "statement": _truncate(statement),
                "rows_affected": cursor.rowcount if cursor.rowcount is not None else -1,
            },
            "duration_ms": duration_ms,
            "executemany": executemany,
        }
        if log_params:
            payload["db"]["params"] = parameters

        if slow_query_threshold_ms > 0:
            bound_logger.warning("slow_query", **payload)
        else:
            bound_logger.debug("db_query", **payload)

    @event.listens_for(sync_engine, "handle_error")
    def _handle_error(exception_context: Any) -> None:
        statement = exception_context.statement
        if statement is None:
            # Connection-level failure with no statement attached — the pool and
            # engine already log those; nothing useful to add here.
            return

        payload: dict[str, Any] = {
            "db": {"statement": _truncate(statement)},
            "error": str(exception_context.original_exception),
        }
        duration_ms = _elapsed_ms(exception_context.execution_context)
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        if log_params and exception_context.parameters is not None:
            payload["db"]["params"] = exception_context.parameters

        bound_logger.warning("db_query_failed", **payload)

    setattr(sync_engine, _INSTALLED_KEY, True)
    return engine
