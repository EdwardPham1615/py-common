"""Declarative mixins for the columns nearly every table repeats."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import ColumnElement

from py_common.utils.ids import new_uuid7

__all__ = [
    "SoftDeleteMixin",
    "TimestampMixin",
    "UUIDv7PrimaryKeyMixin",
]


class UUIDv7PrimaryKeyMixin:
    """A time-ordered UUID primary key.

    UUIDv7 sorts by creation time, so inserts land at the end of the index
    instead of scattering across it the way v4 does — the difference shows up as
    write amplification and index bloat on a large table, not as anything you
    would notice in development.

    The default is generated in Python rather than by the database, so the value
    is known once the session flushes and needs no ``RETURNING`` round trip to
    read back. Note it is a *column* default: the attribute is ``None`` until
    flush, so code that needs the id earlier must call ``session.flush()``
    rather than assume construction assigned one.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=new_uuid7,
    )


class TimestampMixin:
    """``created_at`` / ``updated_at``, both maintained by the database.

    ``server_default``/``onupdate`` use the database clock rather than the
    application's. Several application instances with drifting clocks would
    otherwise write timestamps that do not order consistently — and ordering is
    the main thing these columns are for.

    Stored as ``TIMESTAMP WITH TIME ZONE``. A naive timestamp column silently
    reinterprets its contents when a deployment moves region.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    """``deleted_at`` plus the predicates for filtering on it.

    Deliberately *not* a global query filter. SQLAlchemy can install one, but a
    filter that applies itself to every query is a filter people forget exists:
    reports come out short, joins drop rows, and the cause is invisible at the
    call site. Here the caller writes the predicate, so the exclusion is
    something a reader of the query can see::

        stmt = select(User).where(User.is_active())

    ``deleted_at`` is nullable and indexed, since "the rows that are not
    deleted" is the common lookup on any table that has this column.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    @classmethod
    def is_active(cls) -> ColumnElement[bool]:
        """Predicate matching rows that have not been soft-deleted."""
        return cls.deleted_at.is_(None)

    @classmethod
    def is_deleted(cls) -> ColumnElement[bool]:
        """Predicate matching soft-deleted rows."""
        return cls.deleted_at.is_not(None)

    def mark_deleted(self, *, now: datetime | None = None) -> None:
        """Stamp the row as deleted.

        Takes ``now`` so a caller with a :class:`~py_common.utils.clock.Clock`
        can keep tests deterministic; defaults to the database's ``now()`` at
        flush time rather than the application's clock, matching
        :class:`TimestampMixin`.
        """
        value: Any = now if now is not None else func.now()
        self.deleted_at = value
