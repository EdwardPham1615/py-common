"""Redis async client factory."""

from __future__ import annotations

from redis.asyncio import ConnectionPool, Redis

from py_common.config import RedisSettings


def create_redis(settings: RedisSettings, *, decode_responses: bool = True) -> Redis:
    """Create a Redis client from settings.

    Keep one client per app alive for the process lifetime (e.g. via a
    ``LifespanResource`` that calls ``client.aclose()`` on shutdown). Each call
    builds its own connection pool, so calling it per request would defeat
    pooling entirely.

    Timeouts are set explicitly: redis-py blocks forever by default, so a hung
    connection would pin an event-loop task indefinitely rather than failing
    fast. ``health_check_interval`` pings idle connections before reuse, which
    is what keeps a pool behind a load balancer from handing out sockets the
    balancer has already dropped.
    """
    pool = ConnectionPool.from_url(
        settings.url,
        max_connections=settings.max_connections,
        decode_responses=decode_responses,
        socket_timeout=settings.socket_timeout_seconds,
        socket_connect_timeout=settings.socket_connect_timeout_seconds,
        socket_keepalive=True,
        health_check_interval=settings.health_check_interval_seconds,
        retry_on_timeout=settings.retry_on_timeout,
    )
    return Redis(connection_pool=pool)
