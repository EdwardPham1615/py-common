"""Environment resolution helpers."""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values

ENVIRONMENT_VAR = "ENVIRONMENT"
BASE_ENV_FILE = ".env"


class Environment(StrEnum):
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"


def _coerce(raw: str, *, source: str) -> Environment:
    """Parse an environment name, naming the source in the error.

    Silently falling back to DEV on a typo (e.g. ``prod``) would enable docs and
    relax security in production, so an invalid value raises instead.
    """
    try:
        return Environment(raw.strip().lower())
    except ValueError as exc:
        valid = ", ".join(e.value for e in Environment)
        raise ValueError(
            f"Invalid {ENVIRONMENT_VAR}={raw!r} (from {source}); expected one of: {valid}"
        ) from exc


def resolve_environment(
    environment: str | None = None,
    *,
    base_env_file: str = BASE_ENV_FILE,
) -> Environment:
    """Resolve the active environment from, in order of priority:

    1. the ``environment`` argument
    2. the real ``ENVIRONMENT`` process environment variable
    3. ``ENVIRONMENT`` inside the base ``.env`` file
    4. :attr:`Environment.DEV`

    Step 3 exists because of a chicken-and-egg problem that is otherwise
    silent. Env files are chosen by environment, but the environment is itself
    a setting — and putting ``ENVIRONMENT=production`` in ``.env`` without also
    exporting it is the obvious thing to do. Reading only ``os.environ`` then
    resolves to ``dev``, so ``.env.production`` is never loaded, while the
    ``.env`` file still sets ``settings.environment`` to ``production``. The
    service reports itself as production, passes an ``is_production`` check,
    and runs on development database and broker addresses. Nothing logs a
    warning, because from each component's point of view nothing went wrong.
    """
    if environment is not None:
        return _coerce(environment, source="argument")

    from_process = os.getenv(ENVIRONMENT_VAR)
    if from_process:
        return _coerce(from_process, source=f"{ENVIRONMENT_VAR} environment variable")

    base = Path(base_env_file)
    if base.exists():
        from_file = dotenv_values(base).get(ENVIRONMENT_VAR)
        if from_file:
            return _coerce(from_file, source=base_env_file)

    return Environment.DEV


def resolve_env_files(
    environment: str | None = None,
    *,
    base_env_file: str = BASE_ENV_FILE,
) -> list[str]:
    """Return ordered env files to load (``.env`` then ``.env.{environment}``).

    When passed to pydantic-settings as ``_env_file``, later files take priority,
    so environment-specific values override the base ``.env``.
    """
    env = resolve_environment(environment, base_env_file=base_env_file)
    files = [base_env_file, f"{base_env_file}.{env.value}"]
    return [f for f in files if Path(f).exists()]


@lru_cache
def get_environment() -> Environment:
    """Return the active environment, cached for the life of the process.

    Resolves exactly like :func:`resolve_environment`, so this and
    ``settings.environment`` cannot disagree about which environment the
    service is running in.

    Call :meth:`cache_clear` after changing ``ENVIRONMENT`` or the ``.env``
    file within a process — tests do this; production does not change either
    after start-up.
    """
    return resolve_environment()
