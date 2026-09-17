"""Thin Alembic helpers: config builder, upgrade/downgrade, opt-in lifespan."""

from __future__ import annotations

from typing import Any

from py_common.config import DatabaseSettings
from py_common.logging import get_logger
from py_common.runtime.lifespan import LifespanResource

logger = get_logger(__name__)


def _require_alembic() -> Any:
    try:
        from alembic.config import Config
    except ImportError as exc:
        raise ImportError(
            'Alembic is required for migrations. Install with: pip install "py-common[migrations]"'
        ) from exc
    return Config


def build_alembic_config(
    settings: DatabaseSettings,
    *,
    script_location: str | None = None,
    version_table: str = "alembic_version",
    ini_section: str = "alembic",
) -> Any:
    """Build an in-memory Alembic ``Config`` from :class:`DatabaseSettings`.

    Uses the sync DSN (``postgresql+psycopg://…``) because Alembic migrations
    run synchronously by default.

    ``version_table`` is written to the config, but Alembic only applies it if
    the service's ``env.py`` reads it back and passes it to
    ``context.configure``. The stock ``alembic init`` template does not, so
    setting it here alone changes nothing.
    """
    config_cls = _require_alembic()
    location = script_location or settings.migrations_script_location
    config = config_cls()
    config.set_main_option("script_location", location)
    # Alembic keeps its options in a ConfigParser, which treats % as the start of
    # an interpolation. DSNs are full of them: DatabaseSettings percent-encodes
    # the credentials, so any password containing @ : / or a space arrives here
    # as %40, %3A, %2F, %20 — and configparser rejects the value outright rather
    # than mangling it, taking every migration entry point down with it. Doubling
    # restores the original string on read.
    config.set_main_option("sqlalchemy.url", settings.sync_dsn.replace("%", "%%"))
    config.set_main_option("version_table", version_table)
    config.config_ini_section = ini_section
    return config


def upgrade_to_head(
    settings: DatabaseSettings,
    *,
    script_location: str | None = None,
) -> None:
    """Run ``alembic upgrade head`` against ``settings.sync_dsn``."""
    from alembic import command

    config = build_alembic_config(settings, script_location=script_location)
    logger.info(
        "alembic_upgrade_head",
        script_location=script_location or settings.migrations_script_location,
    )
    command.upgrade(config, "head")


def downgrade(
    settings: DatabaseSettings,
    *,
    script_location: str | None = None,
    revision: str = "-1",
) -> None:
    """Run ``alembic downgrade <revision>`` (default: one revision)."""
    from alembic import command

    config = build_alembic_config(settings, script_location=script_location)
    logger.info(
        "alembic_downgrade",
        revision=revision,
        script_location=script_location or settings.migrations_script_location,
    )
    command.downgrade(config, revision)


def current_revision(
    settings: DatabaseSettings,
    *,
    script_location: str | None = None,
    version_table: str = "alembic_version",
) -> str | None:
    """Return the revision stamped in the database, or ``None`` if unmigrated.

    Returns rather than prints. The previous implementation delegated to
    ``alembic current``, which writes through Alembic's own output plumbing —
    so a caller got ``None`` back and, depending on how logging was configured,
    nothing on stdout either. A revision is worth having as a value: services
    log it at startup, deploy jobs assert on it, and health endpoints report it.

    Pass ``version_table`` if the service's ``env.py`` configures a
    non-default one.
    """
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    engine = create_engine(settings.sync_dsn)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"version_table": version_table})
            return context.get_current_revision()
    finally:
        engine.dispose()


def migration_lifespan_resource(
    settings: DatabaseSettings,
    *,
    script_location: str | None = None,
    name: str = "migrations",
) -> LifespanResource:
    """Opt-in Alembic upgrade on startup when ``settings.auto_migrate`` is True.

    Keep ``auto_migrate=False`` in production and run migrations from a deploy
    job instead (avoids multi-instance race conditions).
    """
    location = script_location or settings.migrations_script_location

    async def startup() -> None:
        if not settings.auto_migrate:
            logger.info("migrations_skipped", reason="auto_migrate_disabled")
            return
        upgrade_to_head(settings, script_location=location)

    return LifespanResource(name=name, startup=startup)
