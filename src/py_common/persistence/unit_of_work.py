"""Unit of Work abstract interface (same-engine transactions)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Self


class UnitOfWork(ABC):
    """Coordinates a single atomic unit of work against one persistence engine.

    Cross-engine coordination (e.g. Mongo + Postgres) is intentionally out of
    scope — document that limitation at the call site until a saga/outbox is added.
    """

    @abstractmethod
    async def __aenter__(self) -> Self:
        """Open the underlying session / transaction."""

    @abstractmethod
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Commit on success; rollback on failure; always release resources."""

    @abstractmethod
    async def commit(self) -> None:
        """Persist pending changes."""

    @abstractmethod
    async def rollback(self) -> None:
        """Discard pending changes."""
