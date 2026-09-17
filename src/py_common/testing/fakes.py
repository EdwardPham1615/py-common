"""In-memory fakes for persistence abstractions (for service test suites)."""

from __future__ import annotations

from collections.abc import Sequence
from operator import attrgetter
from types import TracebackType
from typing import Any, Self

from py_common.persistence.repository import Repository
from py_common.persistence.unit_of_work import UnitOfWork

OrderBy = str | Sequence[str] | None

_ORDER_BY_ERROR = (
    "InMemoryRepository orders by attribute name, got {value!r}. "
    "Pass 'created_at' or '-created_at' rather than a SQLAlchemy expression."
)


def _sorted_by(items: list[Any], ordering: Any) -> list[Any]:
    """Sort by attribute name(s); a leading ``-`` means descending."""
    if isinstance(ordering, str):
        fields: list[Any] = [ordering]
    elif isinstance(ordering, (list, tuple)):
        fields = list(ordering)
    else:
        # Not `list(ordering)`: a SQLAlchemy column is iterable-ish enough to
        # blow up deep inside the ORM instead of saying what went wrong.
        raise TypeError(_ORDER_BY_ERROR.format(value=ordering))

    # Least-significant key first: Python's sort is stable, so successive passes
    # compose into a multi-key sort while letting each key pick its own
    # direction — which a single tuple key cannot do.
    for field in reversed(fields):
        if not isinstance(field, str):
            raise TypeError(_ORDER_BY_ERROR.format(value=field))
        descending = field.startswith("-")
        items = sorted(items, key=attrgetter(field.removeprefix("-")), reverse=descending)
    return items


class InMemoryRepository[EntityT, IdT](Repository[EntityT, IdT]):
    """Dict-backed repository. Entities must expose the ``id_attr`` attribute.

    Ordering is expressed as attribute names — ``"name"``, or ``"-created_at"``
    for descending — because a fake has no SQL to sort with. Pass a SQLAlchemy
    column expression and it raises rather than ignoring the argument, since a
    silently unsorted page is a test that passes while asserting nothing.
    """

    def __init__(self, *, id_attr: str = "id", default_order_by: OrderBy = None) -> None:
        self._id_attr = id_attr
        self._items: dict[IdT, EntityT] = {}
        self._default_order_by = default_order_by

    def _id_of(self, entity: EntityT) -> IdT:
        entity_id: IdT = getattr(entity, self._id_attr)
        return entity_id

    async def create(self, entity: EntityT) -> EntityT:
        self._items[self._id_of(entity)] = entity
        return entity

    async def get(self, entity_id: IdT) -> EntityT | None:
        return self._items.get(entity_id)

    async def get_list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        order_by: Any | None = None,
    ) -> Sequence[EntityT]:
        items = list(self._items.values())
        ordering = order_by if order_by is not None else self._default_order_by
        if ordering is not None:
            items = _sorted_by(items, ordering)
        return items[offset : offset + limit]

    async def update(self, entity: EntityT) -> EntityT:
        self._items[self._id_of(entity)] = entity
        return entity

    async def delete(self, entity_id: IdT) -> bool:
        return self._items.pop(entity_id, None) is not None


class FakeUnitOfWork(UnitOfWork):
    """Records commit/rollback calls so tests can assert transaction behavior."""

    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> Self:
        self.entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.exited = True
        if exc_type is not None:
            await self.rollback()
        else:
            await self.commit()

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


__all__: list[str] = ["FakeUnitOfWork", "InMemoryRepository"]
