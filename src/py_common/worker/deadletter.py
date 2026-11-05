"""The dead-letter list: tasks that ran out of retries, kept so they can be run again."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from celery import Celery
    from redis.asyncio import Redis


def dead_letter_entry(task_name: str, args: Any, error: BaseException, now: float) -> str:
    """One dead letter, as stored: enough to send the task again, and to see why it died."""
    return json.dumps(
        {
            "task": task_name,
            "args": list(args or []),
            "error": f"{type(error).__name__}: {error}",
            "failed_at": int(now * 1000),
        },
        default=str,
        ensure_ascii=False,
    )


async def list_dead_letters(redis: Redis, key: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """The oldest ``limit`` dead letters, without removing them."""
    # Typed Any: redis' stubs declare commands as `Awaitable[T] | T` in some releases and
    # as `Awaitable[T]` in others, and a cast is "redundant" in one and needed in the other.
    pending: Any = redis.lrange(key, 0, max(limit, 1) - 1)
    raw: list[str] = await pending
    return [json.loads(item) for item in raw]


async def requeue_dead_letters(
    celery: Celery, redis: Redis, key: str, *, limit: int | None = None
) -> int:
    """Send dead letters back to the broker, oldest first, and return how many went.

    Each is removed *before* it is sent and put back if sending fails, so a crash
    between the two loses nothing it did not already lose -- but a letter can run
    twice, so tasks must be idempotent (they have to be anyway: workers acknowledge
    late). Stops at ``limit`` when given, otherwise drains the list.
    """
    sent = 0
    while limit is None or sent < limit:
        pop: Any = redis.lpop(key)
        item: str | None = await pop
        if item is None:
            break
        entry = json.loads(item)
        try:
            celery.send_task(entry["task"], args=entry["args"])
        except Exception:
            push: Any = redis.lpush(key, item)
            await push
            raise
        sent += 1
    return sent
