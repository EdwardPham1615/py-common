"""Generic repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from py_common.errors import AppError


class Repository[EntityT, IdT](ABC):
    """Single-engine persistence contract.

    Implementations must keep each method single-purpose. Domain-specific
    queries (e.g. ``get_by_email``) live on concrete repositories, not here.
    """

    @abstractmethod
    async def create(self, entity: EntityT) -> EntityT:
        """Persist a new entity and return it (with generated fields populated)."""

    @abstractmethod
    async def get(self, entity_id: IdT) -> EntityT | None:
        """Fetch one entity by primary key, or ``None`` if missing."""

    async def get_or_raise(self, entity_id: IdT, *, detail: str | None = None) -> EntityT:
        """Fetch one entity by primary key, or raise :class:`~py_common.errors.AppError`.

        The four lines this replaces — fetch, test for ``None``, raise, return —
        were written once per id-based route in every service, and again in every
        gRPC servicer, which has no dependency injection to hide them behind.

        Concrete rather than abstract, so every implementation inherits it
        unchanged: the in-memory fake gets the same behaviour as the SQLAlchemy
        repository for free, which is the only way the two stay substitutable
        without anyone maintaining the pairing.

        ``AppError`` rather than an HTTP exception, because this layer does not
        know it is behind HTTP. ``ErrorCode.NOT_FOUND`` becomes a 404 only in
        ``py_common.http``; a gRPC servicer maps the same error to ``NOT_FOUND``.

        ``detail`` reaches the client verbatim as the problem document's
        ``detail``. Omitted, the response carries the generic "Not Found" title
        and no detail at all.

        A caller needing a different error — a 403 to avoid confirming that a
        resource exists, say — still writes the explicit form. That case has no
        parameter here yet on purpose: adding one later is additive, whereas
        shipping ``error=`` with nothing using it would be surface this library
        has to keep.
        """
        entity = await self.get(entity_id)
        if entity is None:
            raise AppError.not_found(detail)
        return entity

    @abstractmethod
    async def get_list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        order_by: Any | None = None,
    ) -> Sequence[EntityT]:
        """Return a page of entities.

        ``order_by`` is deliberately untyped: each backend speaks its own
        ordering language — SQLAlchemy column expressions for
        :class:`~py_common.persistence.sqlalchemy_repository.SqlAlchemyRepository`,
        attribute names for
        :class:`~py_common.testing.fakes.InMemoryRepository`. It belongs on the
        interface even so, because leaving it off the contract is what let the
        in-memory fake quietly stop being substitutable for the real repository
        in any test that ordered its results.
        """

    @abstractmethod
    async def update(self, entity: EntityT) -> EntityT:
        """Persist mutations already applied to ``entity`` and return the refreshed row."""

    @abstractmethod
    async def delete(self, entity_id: IdT) -> bool:
        """Delete by primary key. Returns ``True`` if a row was deleted, else ``False``."""
