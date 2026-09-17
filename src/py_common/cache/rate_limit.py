"""Rate limiting primitives with Redis and in-memory backends.

Two Redis strategies are available:

- :class:`RedisRateLimiter` — fixed window. One counter per key, cheapest
  possible, but allows up to ``2 * times`` requests across a window boundary.
- :class:`RedisSlidingWindowRateLimiter` — sliding window log. Smooth, no
  boundary burst, at the cost of one sorted-set entry per allowed request.

Both fail **open** by default: a Redis outage degrades the limit rather than
taking the API down with it. Set ``fail_open=False`` where the limit protects
something that must not be exceeded (payment retries, SMS sending).
"""

from __future__ import annotations

import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from py_common.logging import get_logger

logger = get_logger(__name__)

# Fixed window: one INCR, TTL set only on the first hit of the window.
_INCR_WITH_TTL = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('PTTL', KEYS[1])
return {count, ttl}
"""

# Sliding window log. Scores come from Redis TIME, not the caller's clock, so
# instances with skewed clocks cannot corrupt a shared window. Denied requests
# are not recorded — a client hammering a closed limit must not keep extending it.
_SLIDING_WINDOW = """
local t = redis.call('TIME')
local now_ms = (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
local window_ms = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local member = ARGV[3]

redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now_ms - window_ms)
local count = redis.call('ZCARD', KEYS[1])

if count < limit then
    redis.call('ZADD', KEYS[1], now_ms, member)
    redis.call('PEXPIRE', KEYS[1], window_ms)
    return {1, count + 1, window_ms}
end

local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
local reset_ms = window_ms
if oldest[2] then
    reset_ms = (tonumber(oldest[2]) + window_ms) - now_ms
end
redis.call('PEXPIRE', KEYS[1], window_ms)
return {0, count, reset_ms}
"""

_RATE_PATTERN = re.compile(r"^(\d+)\s*(?:/|\s+per\s+)\s*(\d+)?\s*([a-zA-Z]+)$")
_UNIT_SECONDS: dict[str, float] = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}


def parse_rate(rate: str | tuple[int, float]) -> tuple[int, float]:
    """Parse a rate into ``(times, seconds)``.

    Accepts ``"100/minute"``, ``"10/15seconds"``, ``"100 per 2 minutes"``,
    ``"5/s"``, or an explicit ``(times, seconds)`` tuple. Reading a limit is
    where mistakes hide, and ``"100/minute"`` is harder to misread than
    ``times=100, seconds=60``.
    """
    if isinstance(rate, tuple):
        times, seconds = rate
        if times < 1 or seconds <= 0:
            raise ValueError(f"Invalid rate {rate!r}: times must be >= 1 and seconds > 0")
        return int(times), float(seconds)

    match = _RATE_PATTERN.match(rate.strip())
    if match is None:
        raise ValueError(f"Invalid rate {rate!r}; expected e.g. '100/minute' or '10 per 5 seconds'")

    times = int(match.group(1))
    multiple = int(match.group(2)) if match.group(2) else 1
    unit = match.group(3).lower()
    if unit not in _UNIT_SECONDS:
        valid = ", ".join(sorted(set(_UNIT_SECONDS)))
        raise ValueError(f"Unknown rate unit {unit!r} in {rate!r}; expected one of: {valid}")
    if times < 1 or multiple < 1:
        raise ValueError(f"Invalid rate {rate!r}: counts must be >= 1")

    return times, multiple * _UNIT_SECONDS[unit]


@dataclass(slots=True)
class RateLimitResult:
    """Outcome of one rate-limit check.

    ``degraded`` is True when the backend was unreachable and ``allowed`` is a
    fail-open guess rather than a real count. Surface it in metrics: degraded
    traffic must not be indistinguishable from traffic that genuinely passed
    the limit.
    """

    allowed: bool
    remaining: int
    reset_after_seconds: float
    limit: int = 0
    degraded: bool = False


class RateLimiter(Protocol):
    async def hit(self, key: str, *, times: int, seconds: float) -> RateLimitResult:
        """Register one hit for ``key``; report whether it is within ``times`` per ``seconds``."""
        ...


def _fail_open(times: int, seconds: float) -> RateLimitResult:
    return RateLimitResult(
        allowed=True,
        remaining=times,
        reset_after_seconds=seconds,
        limit=times,
        degraded=True,
    )


@dataclass
class RedisRateLimiter:
    """Fixed-window limiter shared across all instances (one Redis counter per key).

    Cheapest strategy, but a client can send ``times`` requests just before a
    window boundary and ``times`` more just after. Use
    :class:`RedisSlidingWindowRateLimiter` where that burst matters.
    """

    client: Redis
    prefix: str = "rate_limit"
    fail_open: bool = True

    def __post_init__(self) -> None:
        # register_script sends the body once and uses EVALSHA thereafter,
        # instead of shipping the whole script on every request.
        self._script = self.client.register_script(_INCR_WITH_TTL)

    async def hit(self, key: str, *, times: int, seconds: float) -> RateLimitResult:
        window_ms = int(seconds * 1000)
        try:
            count, ttl_ms = await self._script(keys=[f"{self.prefix}:{key}"], args=[window_ms])
        except RedisError:
            if not self.fail_open:
                raise
            logger.warning("rate_limit_backend_unavailable", key=key, fail_open=True)
            return _fail_open(times, seconds)

        reset_after = (ttl_ms / 1000) if ttl_ms and ttl_ms > 0 else seconds
        return RateLimitResult(
            allowed=count <= times,
            remaining=max(times - count, 0),
            reset_after_seconds=reset_after,
            limit=times,
        )


@dataclass
class RedisSlidingWindowRateLimiter:
    """Sliding-window-log limiter: no burst at window boundaries.

    Keeps one sorted-set member per allowed request within the window, so it
    costs more memory than :class:`RedisRateLimiter` — size the limit
    accordingly before applying it to very high-volume keys.
    """

    client: Redis
    prefix: str = "rate_limit"
    fail_open: bool = True

    def __post_init__(self) -> None:
        self._script = self.client.register_script(_SLIDING_WINDOW)

    async def hit(self, key: str, *, times: int, seconds: float) -> RateLimitResult:
        window_ms = int(seconds * 1000)
        try:
            allowed, count, reset_ms = await self._script(
                keys=[f"{self.prefix}:{key}"],
                args=[window_ms, times, uuid.uuid4().hex],
            )
        except RedisError:
            if not self.fail_open:
                raise
            logger.warning("rate_limit_backend_unavailable", key=key, fail_open=True)
            return _fail_open(times, seconds)

        return RateLimitResult(
            allowed=bool(allowed),
            remaining=max(times - int(count), 0),
            reset_after_seconds=max(float(reset_ms) / 1000, 0.0),
            limit=times,
        )


@dataclass
class InMemoryRateLimiter:
    """Per-process fixed-window limiter for dev and tests (not shared across instances).

    Bounded LRU: without a cap the window map grows by one entry per distinct
    key — per client IP, in practice — and never shrinks. Re-hitting a key
    resets its expired window in place; once ``max_keys`` is reached, the least
    recently used entries are evicted.
    """

    max_keys: int = 10_000
    _windows: OrderedDict[str, tuple[float, int]] = field(default_factory=OrderedDict)

    async def hit(self, key: str, *, times: int, seconds: float) -> RateLimitResult:
        now = time.monotonic()
        window_start, count = self._windows.get(key, (now, 0))
        if now - window_start >= seconds:
            window_start, count = now, 0
        count += 1

        self._windows[key] = (window_start, count)
        self._windows.move_to_end(key)
        while len(self._windows) > self.max_keys:
            self._windows.popitem(last=False)

        return RateLimitResult(
            allowed=count <= times,
            remaining=max(times - count, 0),
            reset_after_seconds=max(seconds - (now - window_start), 0.0),
            limit=times,
        )
