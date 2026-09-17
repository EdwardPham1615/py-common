"""Async retry backed by tenacity (exponential backoff + jitter)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from py_common.logging import get_logger

logger = get_logger(__name__)


def _before_sleep(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(
        "retrying_after_error",
        attempt=retry_state.attempt_number,
        delay_seconds=round(retry_state.next_action.sleep, 3) if retry_state.next_action else None,
        error=str(exc) if exc else None,
    )


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    initial_backoff_seconds: float = 0.1,
    max_backoff_seconds: float = 10.0,
    jitter: bool = True,
    retry_on: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Call ``fn`` up to ``max_attempts`` times with exponential backoff.

    The last exception is re-raised when all attempts fail — errors are never
    swallowed. ``retry_on`` narrows which exceptions are retryable; anything
    else propagates immediately.

    Usage::

        result = await retry_async(
            lambda: client.get("/flaky"),
            max_attempts=5,
            retry_on=(httpx.TransportError,),
        )
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    wait = (
        wait_exponential_jitter(
            initial=initial_backoff_seconds,
            max=max_backoff_seconds,
        )
        if jitter
        else wait_exponential_jitter(
            initial=initial_backoff_seconds,
            max=max_backoff_seconds,
            jitter=0,
        )
    )

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait,
        retry=retry_if_exception_type(retry_on),
        before_sleep=_before_sleep,
        reraise=True,
    ):
        with attempt:
            return await fn()

    raise AssertionError("unreachable")


def standard_retry(
    *,
    max_attempts: int = 3,
    initial_backoff_seconds: float = 0.1,
    max_backoff_seconds: float = 10.0,
    retry_on: tuple[type[Exception], ...] = (Exception,),
) -> Any:
    """Decorator preset — wraps ``tenacity.retry`` with our standard policy.

    Usage::

        @standard_retry(max_attempts=5, retry_on=(httpx.TransportError,))
        async def fetch():
            ...
    """
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(
            initial=initial_backoff_seconds,
            max=max_backoff_seconds,
        ),
        retry=retry_if_exception_type(retry_on),
        before_sleep=_before_sleep,
        reraise=True,
    )
