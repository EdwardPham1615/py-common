"""Generic repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any


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
