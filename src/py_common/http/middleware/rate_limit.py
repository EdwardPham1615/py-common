"""FastAPI dependency for per-route rate limiting.

Requires a limiter from ``py_common.cache`` (Redis-backed for multi-instance
deployments, in-memory for dev/tests).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request, Response, status

from py_common.cache.rate_limit import RateLimiter, parse_rate
from py_common.http.middleware.request_context import client_ip

KeyFunc = Callable[[Request], str | None]


def _default_key(request: Request) -> str | None:
    """Key by authenticated user when available, else by client IP; scoped per route.

    The IP fallback is what protects unauthenticated endpoints — login,
    register, password reset — so it has to be the *caller's* address. Behind a
    proxy that means configuring ``forwarded_allow_ips`` (see :func:`client_ip`):
    without it every anonymous caller shares the ingress address and therefore
    one bucket, turning a per-client limit into a global one that a single noisy
    client can exhaust for everyone.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    user = getattr(request.state, "user", None)
    identity = getattr(user, "sub", None)
    if identity is None:
        identity = client_ip(request.scope)
    if identity is None:
        return None
    return f"{request.method}:{path}:{identity}"


def build_rate_limit_dep(
    limiter: RateLimiter,
    rate: str | tuple[int, float] | None = None,
    *,
    times: int | None = None,
    seconds: float | None = None,
    key_func: KeyFunc | None = None,
) -> Callable[..., Any]:
    """Build a dependency enforcing a request rate per caller.

    Give the rate either as a readable string or as explicit numbers::

        limiter = RedisRateLimiter(redis)
        rate_limited = build_rate_limit_dep(limiter, "10/second")
        strict = build_rate_limit_dep(limiter, times=10, seconds=1)

        @router.get("/items", dependencies=[Depends(rate_limited)])
        async def list_items(): ...

    Every response carries ``X-RateLimit-Limit`` / ``-Remaining`` / ``-Reset``
    so clients can pace themselves instead of discovering the limit by hitting
    it; 429s add ``Retry-After``.
    """
    if rate is not None:
        limit, window = parse_rate(rate)
    elif times is not None and seconds is not None:
        limit, window = parse_rate((times, seconds))
    else:
        raise ValueError("Pass a rate (e.g. '10/second') or both times= and seconds=")

    get_key = key_func or _default_key

    async def dependency(request: Request, response: Response) -> None:
        key = get_key(request)
        if key is None:
            return
        result = await limiter.hit(key, times=limit, seconds=window)

        reset_after = math.ceil(result.reset_after_seconds)
        headers = {
            "X-RateLimit-Limit": str(result.limit or limit),
            "X-RateLimit-Remaining": str(result.remaining),
            "X-RateLimit-Reset": str(int(time.time()) + reset_after),
        }
        response.headers.update(headers)

        if not result.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
                headers={**headers, "Retry-After": str(reset_after)},
            )

    return dependency
