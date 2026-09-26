"""Persistence abstractions: engine factory, repository interface, and unit of work.

Re-exports resolve lazily, for the reason given in :mod:`py_common.runtime`:
Python executes this file whenever anything imports one of its submodules, so
eager re-exports charge every importer for the union of this package's
dependencies.

The concrete failure this fixes: ``py_common.persistence.migrations`` promises to
work on the ``migrations`` extra (Alembic + psycopg, both synchronous), but
importing it ran this file, which imported ``engine`` and therefore
``sqlalchemy.ext.asyncio`` — and that needs ``greenlet``, which only the
``persistence`` extra's ``sqlalchemy[asyncio]`` brings. `[migrations]` alone
raised *"The SQLAlchemy asyncio module requires that the Python 'greenlet' library
is installed"*. It went unnoticed because it depends on what a fresh resolve
happens to pull in; `scripts/check-extras-isolation.sh` resolves fresh, which is
how it surfaced.

``from py_common.persistence import Repository`` is unchanged; the submodule loads
the first time the name is read.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from py_common.persistence.base import NAMING_CONVENTION as NAMING_CONVENTION
    from py_common.persistence.base import Base as Base
    from py_common.persistence.base import metadata as metadata
    from py_common.persistence.engine import (
        create_engine_and_sessionmaker as create_engine_and_sessionmaker,
    )
    from py_common.persistence.engine import (
        database_lifespan_resource as database_lifespan_resource,
    )
    from py_common.persistence.migrations import build_alembic_config as build_alembic_config
    from py_common.persistence.migrations import current_revision as current_revision
    from py_common.persistence.migrations import downgrade as downgrade
    from py_common.persistence.migrations import (
        migration_lifespan_resource as migration_lifespan_resource,
    )
    from py_common.persistence.migrations import upgrade_to_head as upgrade_to_head
    from py_common.persistence.mixins import SoftDeleteMixin as SoftDeleteMixin
    from py_common.persistence.mixins import TimestampMixin as TimestampMixin
    from py_common.persistence.mixins import UUIDv7PrimaryKeyMixin as UUIDv7PrimaryKeyMixin
    from py_common.persistence.pagination import paginate_cursor as paginate_cursor
    from py_common.persistence.pagination import paginate_offset as paginate_offset
    from py_common.persistence.query_logging import install_query_logger as install_query_logger
    from py_common.persistence.repository import Repository as Repository
    from py_common.persistence.sqlalchemy_repository import (
        SqlAlchemyRepository as SqlAlchemyRepository,
    )
    from py_common.persistence.sqlalchemy_uow import SqlAlchemyUnitOfWork as SqlAlchemyUnitOfWork
    from py_common.persistence.unit_of_work import UnitOfWork as UnitOfWork

# name -> defining submodule. Grouped so the cost of each is visible: `base`,
# `mixins`, `repository` and `unit_of_work` are plain SQLAlchemy Core or ABCs;
# `engine`, `query_logging`, `sqlalchemy_repository` and `sqlalchemy_uow` need the
# asyncio stack; `migrations` needs Alembic; `pagination` reaches into
# `py_common.http` for the cursor codec.
_LAZY: dict[str, str] = {
    "NAMING_CONVENTION": "py_common.persistence.base",
    "Base": "py_common.persistence.base",
    "metadata": "py_common.persistence.base",
    "create_engine_and_sessionmaker": "py_common.persistence.engine",
    "database_lifespan_resource": "py_common.persistence.engine",
    "build_alembic_config": "py_common.persistence.migrations",
    "current_revision": "py_common.persistence.migrations",
    "downgrade": "py_common.persistence.migrations",
    "migration_lifespan_resource": "py_common.persistence.migrations",
    "upgrade_to_head": "py_common.persistence.migrations",
    "SoftDeleteMixin": "py_common.persistence.mixins",
    "TimestampMixin": "py_common.persistence.mixins",
    "UUIDv7PrimaryKeyMixin": "py_common.persistence.mixins",
    "paginate_cursor": "py_common.persistence.pagination",
    "paginate_offset": "py_common.persistence.pagination",
    "install_query_logger": "py_common.persistence.query_logging",
    "Repository": "py_common.persistence.repository",
    "SqlAlchemyRepository": "py_common.persistence.sqlalchemy_repository",
    "SqlAlchemyUnitOfWork": "py_common.persistence.sqlalchemy_uow",
    "UnitOfWork": "py_common.persistence.unit_of_work",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:
    """PEP 562 hook: resolve a re-export on first access."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module), name)


def __dir__() -> list[str]:
    return __all__
