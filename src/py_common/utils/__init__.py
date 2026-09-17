"""Small shared utilities: retry, IDs, clock, circuit breaker."""

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
    "SystemClock",
    "new_nanoid",
    "new_uuid7",
    "retry_async",
    "standard_retry",
    "utcnow",
]


def utcnow() -> datetime:
    """Timezone-aware UTC now — alias of :meth:`SystemClock.now`."""
    return SYSTEM_CLOCK.now()
