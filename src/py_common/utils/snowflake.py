"""Snowflake ids: 64-bit integers that grow with time.

Layout, most significant first: 41 bits of milliseconds since an epoch, 10 bits of
worker id, 12 bits of sequence. The sign bit is never set, so an id fits a
PostgreSQL ``bigint`` for about 69 years from the epoch.

**Serialise them as strings in any API a browser reads.** JavaScript numbers are
doubles and hold integers exactly only up to 2**53; a snowflake is routinely above
that, and ``JSON.parse`` rounds it silently. Pydantic: ``Annotated[int, PlainSerializer(str)]``
on the response model, and accept a string on the way in.

The worker id must be unique per *process* that creates ids, not per host: two
workers sharing an id will eventually issue the same one in the same millisecond.
That is why :func:`new_snowflake_id` refuses to guess when nothing configured it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

# 2026-01-01T00:00:00Z. The default exists so a service moving here from its own
# copy keeps issuing ids in the same space; changing it for a database that already
# holds ids would make new ones collide with old ones.
DEFAULT_EPOCH_MS = 1_767_225_600_000

WORKER_BITS = 10
SEQUENCE_BITS = 12
MAX_WORKER_ID = (1 << WORKER_BITS) - 1
MAX_SEQUENCE = (1 << SEQUENCE_BITS) - 1
TIMESTAMP_SHIFT = WORKER_BITS + SEQUENCE_BITS


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


class SnowflakeGenerator:
    """Thread-safe generator for one process. Each process needs its own worker id."""

    def __init__(
        self,
        worker_id: int,
        *,
        epoch_ms: int = DEFAULT_EPOCH_MS,
        clock: Callable[[], int] = _wall_ms,
    ) -> None:
        if not 0 <= worker_id <= MAX_WORKER_ID:
            raise ValueError(f"worker_id must be in 0..{MAX_WORKER_ID}, got {worker_id}")
        self._worker_id = worker_id
        self._epoch_ms = epoch_ms
        self._clock = clock
        self._lock = threading.Lock()
        self._last_ms = -1
        self._sequence = 0

    def next_id(self) -> int:
        with self._lock:
            now = self._clock()
            if now < self._last_ms:
                # Clock went backwards: stay on the last millisecond rather than risk
                # a duplicate. shortcut: a clock stepped far back (not slewed) stalls
                # this generator, once 4096 ids are used up, until the wall clock
                # catches up -- upgrade to raising on a drift bound if that matters.
                now = self._last_ms
            if now == self._last_ms:
                self._sequence = (self._sequence + 1) & MAX_SEQUENCE
                if self._sequence == 0:
                    now = self._wait_next_ms(self._last_ms)
            else:
                self._sequence = 0
            self._last_ms = now
            return (
                ((now - self._epoch_ms) << TIMESTAMP_SHIFT)
                | (self._worker_id << SEQUENCE_BITS)
                | self._sequence
            )

    def _wait_next_ms(self, last_ms: int) -> int:
        now = self._clock()
        while now <= last_ms:
            time.sleep(0.0001)
            now = max(self._clock(), now)
        return now


def snowflake_timestamp_ms(snowflake_id: int, *, epoch_ms: int = DEFAULT_EPOCH_MS) -> int:
    """When an id was issued, in epoch milliseconds."""
    return (snowflake_id >> TIMESTAMP_SHIFT) + epoch_ms


_default: SnowflakeGenerator | None = None


def configure_snowflake(worker_id: int, *, epoch_ms: int = DEFAULT_EPOCH_MS) -> None:
    """Set the process-wide generator. Call once at start-up, from every entry point
    (the API, each worker process) with that process's own worker id."""
    global _default
    _default = SnowflakeGenerator(worker_id, epoch_ms=epoch_ms)


def new_snowflake_id() -> int:
    """The next id from the process-wide generator set by :func:`configure_snowflake`.

    Raises rather than defaulting to worker 0: a process that forgot to configure
    would share an id with whichever other process did the same, and the collisions
    would surface months later as primary-key violations.
    """
    if _default is None:
        raise RuntimeError(
            "configure_snowflake(worker_id) was never called in this process; "
            "every process that creates ids needs its own worker id"
        )
    return _default.next_id()
