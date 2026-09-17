"""Alembic helpers against a real Postgres and a real migration tree.

Nothing covered these before: they need a database *and* a versions directory,
and neither exists in a unit test. The result was that the one entry point a
service calls at deploy time was the least exercised code in the library.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from py_common.config import DatabaseSettings
from py_common.persistence.migrations import (
    build_alembic_config,
    current_revision,
    downgrade,
    migration_lifespan_resource,
    upgrade_to_head,
)

pytestmark = pytest.mark.integration


# The stock env.py an `alembic init` gives a service. Deliberately unmodified:
# testing against a hand-tuned one would prove our env.py works, not theirs.
ENV_PY = """
from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
connectable = engine_from_config(
    config.get_section(config.config_ini_section, {}),
    prefix="sqlalchemy.",
    poolclass=pool.NullPool,
)
with connectable.connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
"""

REV_1 = '''
"""first"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None

def upgrade() -> None:
    op.create_table("alembic_demo", sa.Column("id", sa.Integer, primary_key=True))

def downgrade() -> None:
    op.drop_table("alembic_demo")
'''

REV_2 = '''
"""second"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"

def upgrade() -> None:
    op.add_column("alembic_demo", sa.Column("name", sa.String(50)))

def downgrade() -> None:
    op.drop_column("alembic_demo", "name")
'''


@pytest.fixture
def script_location(tmp_path: Path) -> str:
    root = tmp_path / "alembic"
    (root / "versions").mkdir(parents=True)
    (root / "env.py").write_text(ENV_PY)
    (root / "script.py.mako").write_text("")
    (root / "versions" / "0001_first.py").write_text(REV_1)
    (root / "versions" / "0002_second.py").write_text(REV_2)
    return str(root)


@pytest.fixture
def settings(pg_dsn_sync: str) -> DatabaseSettings:
    from sqlalchemy.engine import make_url

    url = make_url(pg_dsn_sync)
    return DatabaseSettings(
        host=url.host or "localhost",
        port=url.port or 5432,
        user=url.username or "",
        password=url.password or "",
        db=url.database or "",
    )


@pytest.fixture(autouse=True)
def clean_db(settings: DatabaseSettings) -> Iterator[None]:
    def drop() -> None:
        engine = create_engine(settings.sync_dsn)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_demo"))
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        engine.dispose()

    drop()
    yield
    drop()


def _tables(settings: DatabaseSettings) -> set[str]:
    engine = create_engine(settings.sync_dsn)
    with engine.connect() as conn:
        rows = (
            conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
            .scalars()
            .all()
        )
    engine.dispose()
    return set(rows)


def _stamped_revision(settings: DatabaseSettings) -> str | None:
    engine = create_engine(settings.sync_dsn)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    engine.dispose()
    return row


# --- the actual deploy path ----------------------------------------------


def test_upgrade_to_head_applies_every_revision(
    settings: DatabaseSettings, script_location: str
) -> None:
    """The one call a deploy job makes. It had no test at all."""
    upgrade_to_head(settings, script_location=script_location)

    assert "alembic_demo" in _tables(settings)
    assert _stamped_revision(settings) == "0002"


def test_upgrade_is_idempotent(settings: DatabaseSettings, script_location: str) -> None:
    """Deploy jobs get retried, and two instances can race to run this."""
    upgrade_to_head(settings, script_location=script_location)
    upgrade_to_head(settings, script_location=script_location)
    assert _stamped_revision(settings) == "0002"


def test_downgrade_steps_back_one_revision(
    settings: DatabaseSettings, script_location: str
) -> None:
    upgrade_to_head(settings, script_location=script_location)
    downgrade(settings, script_location=script_location)

    assert _stamped_revision(settings) == "0001"
    assert "alembic_demo" in _tables(settings)  # only the column went


def test_current_revision_returns_what_is_applied(
    settings: DatabaseSettings, script_location: str
) -> None:
    """It returns the revision rather than printing it: services log this at
    startup and deploy jobs assert on it."""
    assert current_revision(settings) is None  # nothing migrated yet

    upgrade_to_head(settings, script_location=script_location)
    assert current_revision(settings) == "0002"

    downgrade(settings, script_location=script_location)
    assert current_revision(settings) == "0001"


# --- the percent bug ------------------------------------------------------


@pytest.mark.parametrize("password", ["p@ss", "a b/c:d#e", "100%pure", "plain"])
def test_config_survives_passwords_that_percent_encode(password: str) -> None:
    """Alembic keeps its options in a ConfigParser, which reads % as the start of
    an interpolation — and DatabaseSettings percent-encodes credentials, so any
    password with @ : / or a space arrives as %40, %3A, %2F, %20. Before the
    doubling fix this raised ValueError, taking down every migration entry point
    for a large share of realistic passwords."""
    settings = DatabaseSettings(user="app", password=password, db="x")
    config = build_alembic_config(settings, script_location="/nonexistent")

    assert config.get_main_option("sqlalchemy.url") == settings.sync_dsn


async def test_upgrade_works_with_a_percent_encoded_password(
    settings: DatabaseSettings, script_location: str
) -> None:
    """End to end, not just the config object: a real connection with a password
    that had to be escaped on the way in and unescaped on the way out."""
    engine = create_engine(settings.sync_dsn)
    with engine.begin() as conn:
        conn.execute(text("DROP ROLE IF EXISTS percenty"))
        conn.execute(text("CREATE ROLE percenty LOGIN PASSWORD 'p@ss w/ord' SUPERUSER"))
    engine.dispose()

    try:
        escaped = DatabaseSettings(
            host=settings.host,
            port=settings.port,
            user="percenty",
            password="p@ss w/ord",
            db=settings.db,
        )
        assert "%" in escaped.sync_dsn
        upgrade_to_head(escaped, script_location=script_location)
        assert _stamped_revision(escaped) == "0002"
    finally:
        # The role owns the tables it just created, so it cannot be dropped
        # until they are.
        engine = create_engine(settings.sync_dsn)
        with engine.begin() as conn:
            conn.execute(text("DROP OWNED BY percenty CASCADE"))
            conn.execute(text("DROP ROLE IF EXISTS percenty"))
        engine.dispose()


# --- the lifespan resource ------------------------------------------------


async def test_lifespan_resource_skips_when_auto_migrate_is_off(
    settings: DatabaseSettings, script_location: str
) -> None:
    """The production default. Migrating from every instance on startup races
    when a deployment rolls several pods at once."""
    settings.auto_migrate = False
    resource = migration_lifespan_resource(settings, script_location=script_location)

    await resource.startup()

    assert "alembic_demo" not in _tables(settings)


async def test_lifespan_resource_migrates_when_enabled(
    settings: DatabaseSettings, script_location: str
) -> None:
    settings.auto_migrate = True
    resource = migration_lifespan_resource(settings, script_location=script_location)

    await resource.startup()

    assert _stamped_revision(settings) == "0002"
