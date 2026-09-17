"""Turn a SQLAlchemy ``Select`` into a :class:`~py_common.http.pagination.Page`.

Lives in ``persistence`` rather than ``http`` because it takes a session and a
statement and returns rows: it has no request, so a worker or CLI job can use
it. ``http.pagination`` keeps the envelope and the cursor codec.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from py_common.http.pagination import Page, PageMeta, decode_cursor, encode_cursor

__all__ = ["paginate_cursor", "paginate_offset"]

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def _coerce_key(key_column: Any, raw: Any) -> Any:
    """Convert a cursor value back to the key column's Python type.

    Cursors are JSON, which has no UUID or datetime, so the value goes in as a
    string. Handing that string straight back to a typed column does not fail
    loudly at the boundary — SQLAlchemy's UUID type raises deep inside statement
    execution with an AttributeError about ``.hex``, which reads like a library
    bug rather than a bad cursor.
    """
    try:
        python_type = key_column.type.python_type
    except NotImplementedError:
        return raw
    if isinstance(raw, python_type):
        return raw
    try:
        if python_type is uuid.UUID:
            return uuid.UUID(str(raw))
        if python_type is datetime:
            return datetime.fromisoformat(str(raw))
        if python_type is date:
            return date.fromisoformat(str(raw))
        if python_type in (int, float, str, bytes):
            return python_type(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid pagination cursor") from exc
    return raw


def _clamp(limit: int, max_limit: int) -> int:
    """Bound the page size.

    ``limit`` normally arrives from a query string, and an unbounded one is a
    denial-of-service primitive: ``?limit=10000000`` asks the database for the
    whole table and the serializer for the whole heap.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    return min(limit, max_limit)


async def paginate_offset[T](
    session: AsyncSession,
    stmt: Select[tuple[T]],
    *,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    max_limit: int = MAX_LIMIT,
    with_total: bool = True,
) -> Page[T]:
    """Offset/limit pagination, with an optional total count.

    ``with_total`` is separate because the count is a second query over the
    whole filtered set — cheap on a small table, and the most expensive thing on
    the page once that table is large. Pass ``False`` for infinite-scroll style
    listings that never render a page count.

    Offset pagination is the wrong tool for a feed that changes underneath the
    reader: rows inserted before the current offset shift everything, so items
    are skipped or repeated between pages. Use :func:`paginate_cursor` there.
    """
    limit = _clamp(limit, max_limit)
    if offset < 0:
        raise ValueError("offset must be >= 0")

    total: int | None = None
    if with_total:
        # order_by must go: it is meaningless here, and PostgreSQL rejects a
        # subquery ORDER BY referencing columns the count does not select.
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await session.execute(count_stmt)).scalar_one())

    # One row beyond the page: the cheap way to know whether a next page exists
    # without a second query, and without trusting a total that may be stale.
    result = await session.execute(stmt.limit(limit + 1).offset(offset))
    rows = list(result.scalars().all())
    has_more = len(rows) > limit
    items = rows[:limit]

    return Page[T](
        items=items,
        meta=PageMeta(
            limit=limit,
            offset=offset,
            page=(offset // limit) + 1,
            total_items=total,
            has_more=has_more,
        ),
    )


async def paginate_cursor[T](
    session: AsyncSession,
    stmt: Select[tuple[T]],
    *,
    key_column: Any,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    descending: bool = False,
    max_limit: int = MAX_LIMIT,
) -> Page[T]:
    """Keyset pagination over a single sortable, unique column.

    Stable under concurrent writes, which is the point: the next page is defined
    by "after this key" rather than "skip this many rows", so an insert between
    requests cannot shift a reader's window.

    ``key_column`` must be unique and sortable — a UUIDv7 or bigint primary key
    (see :class:`~py_common.persistence.mixins.UUIDv7PrimaryKeyMixin`). A
    non-unique column such as ``created_at`` silently drops or repeats rows that
    share a value at a page boundary.

    The statement's own ``ORDER BY`` is replaced by the key ordering: keyset
    pagination is only correct when the sort matches the seek predicate.

    Raises ``ValueError`` on a malformed cursor, which callers map to a 400.
    """
    limit = _clamp(limit, max_limit)

    stmt = stmt.order_by(None).order_by(key_column.desc() if descending else key_column.asc())

    if cursor:
        payload = decode_cursor(cursor)
        if "k" not in payload:
            raise ValueError("Invalid pagination cursor")
        last_key = _coerce_key(key_column, payload["k"])
        stmt = stmt.where(key_column < last_key if descending else key_column > last_key)

    result = await session.execute(stmt.limit(limit + 1))
    rows = list(result.scalars().all())
    has_more = len(rows) > limit
    items = rows[:limit]

    next_cursor: str | None = None
    if has_more and items:
        # str(): the cursor is JSON, which has no UUID or datetime. _coerce_key
        # converts it back on the way in, using the column's own Python type —
        # which is why the key must be a real mapped column, not a computed
        # expression.
        next_cursor = encode_cursor({"k": str(getattr(items[-1], key_column.key))})

    return Page[T](
        items=items,
        meta=PageMeta(
            limit=limit,
            next_cursor=next_cursor,
            has_more=has_more,
        ),
    )
