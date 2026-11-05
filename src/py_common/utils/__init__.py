"""Small shared utilities: retry, IDs (nanoid, UUIDv7, snowflake), clock, circuit breaker."""

from __future__ import annotations

from datetime import datetime

from py_common.utils.circuit_breaker import (
    AsyncCircuitBreaker,
    CircuitOpenError,
    CircuitState,
)
from py_common.utils.clock import SYSTEM_CLOCK, Clock, FixedClock, SystemClock
from py_common.utils.ids import (
    ALPHABET_ALPHA,
    ALPHABET_ALPHANUMERIC,
    ALPHABET_DEFAULT,
    ALPHABET_LOWER,
    ALPHABET_NUMERIC,
    ALPHABET_UPPER,
    ALPHABET_UPPER_NUMERIC,
    new_nanoid,
    new_uuid7,
)
from py_common.utils.retry import retry_async, standard_retry
from py_common.utils.snowflake import (
    SnowflakeGenerator,
    configure_snowflake,
    new_snowflake_id,
    snowflake_timestamp_ms,
)

__all__ = [
    "ALPHABET_ALPHA",
    "ALPHABET_ALPHANUMERIC",
    "ALPHABET_DEFAULT",
    "ALPHABET_LOWER",
    "ALPHABET_NUMERIC",
    "ALPHABET_UPPER",
    "ALPHABET_UPPER_NUMERIC",
    "SYSTEM_CLOCK",
    "AsyncCircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "Clock",
    "FixedClock",
    "SnowflakeGenerator",
    "SystemClock",
    "configure_snowflake",
    "new_nanoid",
    "new_snowflake_id",
    "new_uuid7",
    "retry_async",
    "snowflake_timestamp_ms",
    "standard_retry",
    "utcnow",
]


def utcnow() -> datetime:
    """Timezone-aware UTC now — alias of :meth:`SystemClock.now`."""
    return SYSTEM_CLOCK.now()
