"""Injectable clock for production code and deterministic tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Timezone-aware UTC clock. Inject into services so tests can freeze time."""

    def now(self) -> datetime:
        """Return the current UTC-aware datetime."""
        ...


class SystemClock:
    """Real wall-clock time (UTC)."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Frozen clock for tests. Call :meth:`advance` or :meth:`set` to move time."""

    def __init__(self, instant: datetime | None = None) -> None:
        if instant is None:
            instant = datetime.now(UTC)
        elif instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        self._instant = instant

    def now(self) -> datetime:
        return self._instant

    def set(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        self._instant = instant

    def advance(self, *, seconds: float = 0, minutes: float = 0, hours: float = 0) -> None:
        from datetime import timedelta

        self._instant = self._instant + timedelta(
            seconds=seconds,
            minutes=minutes,
            hours=hours,
        )


SYSTEM_CLOCK = SystemClock()
