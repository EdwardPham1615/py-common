"""Persistence abstractions: engine factory, repository interface, and unit of work."""

from py_common.persistence.base import NAMING_CONVENTION, Base, metadata
from py_common.persistence.engine import (
    create_engine_and_sessionmaker,
    database_lifespan_resource,
)
from py_common.persistence.migrations import (
    build_alembic_config,
    current_revision,
    downgrade,
    migration_lifespan_resource,
    upgrade_to_head,
)
from py_common.persistence.mixins import (
    SoftDeleteMixin,
    TimestampMixin,
    UUIDv7PrimaryKeyMixin,
)
from py_common.persistence.pagination import paginate_cursor, paginate_offset
from py_common.persistence.query_logging import install_query_logger
from py_common.persistence.repository import Repository
from py_common.persistence.sqlalchemy_repository import SqlAlchemyRepository
from py_common.persistence.sqlalchemy_uow import SqlAlchemyUnitOfWork
from py_common.persistence.unit_of_work import UnitOfWork

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "Repository",
    "SoftDeleteMixin",
    "SqlAlchemyRepository",
    "SqlAlchemyUnitOfWork",
    "TimestampMixin",
    "UUIDv7PrimaryKeyMixin",
    "UnitOfWork",
    "build_alembic_config",
    "create_engine_and_sessionmaker",
    "current_revision",
    "database_lifespan_resource",
    "downgrade",
    "install_query_logger",
    "metadata",
    "migration_lifespan_resource",
    "paginate_cursor",
    "paginate_offset",
    "upgrade_to_head",
]
