"""Config behavior: nested settings must not read bare env vars; env validation."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel

from py_common.config import (
    BaseAppSettings,
    CorsSettings,
    DatabaseSettings,
    Environment,
    HttpSettings,
    KeycloakSettings,
    OtelSettings,
    ProfilerSettings,
    RedisSettings,
    ServerSettings,
    StorageSettings,
    get_environment,
    resolve_env_files,
)
from py_common.config.environment import resolve_environment


class _Settings(BaseAppSettings):
    postgres: DatabaseSettings = DatabaseSettings()


def test_nested_settings_ignore_bare_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    # $USER always exists in shells — it must never leak into DatabaseSettings.user.
    monkeypatch.setenv("USER", "shell-user")
    monkeypatch.setenv("HOST", "shell-host")
    db = DatabaseSettings()
    assert db.user == "postgres"
    assert db.host == "localhost"


def test_nested_settings_populated_via_delimiter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES__HOST", "db.internal")
    monkeypatch.setenv("POSTGRES__PASSWORD", "s3cret")
    settings = _Settings(_env_file=None)
    assert settings.postgres.host == "db.internal"
    assert settings.postgres.password == "s3cret"


def test_nested_settings_are_plain_models() -> None:
    assert issubclass(DatabaseSettings, BaseModel)
    from pydantic_settings import BaseSettings

    assert not issubclass(DatabaseSettings, BaseSettings)


def test_dsn_quotes_credentials() -> None:
    db = DatabaseSettings(user="app@svc", password="p@ss:word/1")
    assert "app%40svc" in db.async_dsn
    assert "p%40ss%3Aword%2F1" in db.async_dsn


@pytest.mark.parametrize("password", ["p@ss w/ord", "sp ace", "a:b/c@d#e", "100%pure", "simple"])
def test_dsn_round_trips_through_a_url_parser(password: str) -> None:
    """The DSN is only useful if a driver reads back the credentials that went in.

    quote_plus encodes a space as "+", and no URL parser turns that back into a
    space -- so a password with a space in it authenticated as something else and
    the service could not connect at all. Asserting on the encoded substring
    would not have caught that; asserting on what a parser recovers does.
    """
    from sqlalchemy.engine import make_url

    db = DatabaseSettings(user="ap p", password=password)
    for dsn in (db.async_dsn, db.sync_dsn):
        url = make_url(dsn)
        assert url.password == password
        assert url.username == "ap p"


def test_keycloak_urls() -> None:
    kc = KeycloakSettings(server_url="http://kc:8080/", realm="myrealm")
    assert kc.issuer == "http://kc:8080/realms/myrealm"
    assert kc.jwks_url.endswith("/protocol/openid-connect/certs")


def test_get_environment_rejects_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    get_environment.cache_clear()
    monkeypatch.setenv("ENVIRONMENT", "prod")  # typo of "production"
    with pytest.raises(ValueError, match="Invalid ENVIRONMENT"):
        get_environment()
    get_environment.cache_clear()


def test_get_environment_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    get_environment.cache_clear()
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert get_environment() is Environment.STAGING
    get_environment.cache_clear()


class _EnvSettings(BaseAppSettings):
    api_key: str = "unset"


@pytest.fixture
def env_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Run in an empty directory so stray .env files cannot reach the test."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    get_environment.cache_clear()
    yield tmp_path
    get_environment.cache_clear()


def test_environment_read_from_env_file_when_not_exported(env_dir: Path) -> None:
    """The bug this whole resolution order exists for.

    ``ENVIRONMENT=production`` declared only in ``.env`` used to resolve to
    ``dev``, so ``.env.production`` was never loaded — while ``.env`` still set
    ``settings.environment`` to production. The service passed an
    ``is_production`` check while running on dev infrastructure.
    """
    (env_dir / ".env").write_text("ENVIRONMENT=production\nAPI_KEY=from-base\n")
    (env_dir / ".env.production").write_text("API_KEY=from-production\n")
    (env_dir / ".env.dev").write_text("API_KEY=from-dev\n")

    assert resolve_environment() is Environment.PRODUCTION
    assert resolve_env_files() == [".env", ".env.production"]

    settings = _EnvSettings()
    assert settings.environment is Environment.PRODUCTION
    assert settings.api_key == "from-production"


def test_process_environment_beats_env_file(env_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real env var outranks .env — that is how a deployment overrides an image."""
    (env_dir / ".env").write_text("ENVIRONMENT=dev\n")
    (env_dir / ".env.staging").write_text("API_KEY=from-staging\n")
    monkeypatch.setenv("ENVIRONMENT", "staging")

    assert resolve_environment() is Environment.STAGING
    assert _EnvSettings().api_key == "from-staging"


def test_defaults_to_dev_without_any_signal(env_dir: Path) -> None:
    assert resolve_environment() is Environment.DEV
    assert resolve_env_files() == []


def test_invalid_environment_in_env_file_raises(env_dir: Path) -> None:
    """A typo must not silently downgrade to dev, and the error must name the file."""
    (env_dir / ".env").write_text("ENVIRONMENT=prod\n")
    with pytest.raises(ValueError, match=r"Invalid ENVIRONMENT.*from \.env"):
        resolve_environment()


def test_get_environment_agrees_with_settings(env_dir: Path) -> None:
    """One environment, not two — the whole point of routing both through
    ``resolve_environment``."""
    (env_dir / ".env").write_text("ENVIRONMENT=staging\n")
    get_environment.cache_clear()
    assert get_environment() is _EnvSettings().environment is Environment.STAGING


def test_mismatch_between_resolved_files_and_settings_raises(env_dir: Path) -> None:
    """An environment-specific file that reassigns ENVIRONMENT is the one way the
    two can still diverge: files were picked for staging, settings say production."""
    (env_dir / ".env").write_text("ENVIRONMENT=staging\n")
    (env_dir / ".env.staging").write_text("ENVIRONMENT=production\n")

    with pytest.raises(ValueError, match="Environment mismatch"):
        _EnvSettings()


def test_explicit_env_file_skips_resolution(env_dir: Path) -> None:
    """``_env_file`` is an escape hatch; passing it must not trigger the guard."""
    (env_dir / "custom.env").write_text("ENVIRONMENT=production\n")
    settings = _EnvSettings(_env_file=str(env_dir / "custom.env"))
    assert settings.environment is Environment.PRODUCTION


def test_middleware_settings_come_from_nested_env_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of moving these out of function arguments: an operator changes a
    timeout by setting an environment variable, not by asking for a release."""
    monkeypatch.setenv("HTTP__TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("HTTP__HSTS", "false")
    monkeypatch.setenv("HTTP__CONTENT_SECURITY_POLICY", "default-src 'none'")
    monkeypatch.setenv("SERVER__PORT", "9000")
    monkeypatch.setenv("SERVER__DRAIN_DELAY_SECONDS", "8")
    monkeypatch.setenv("SERVER__FORWARDED_ALLOW_IPS", "10.0.0.0/8")

    settings = BaseAppSettings(_env_file=None)

    assert settings.http.timeout_seconds == 12.5
    assert settings.http.hsts is False
    assert settings.http.content_security_policy == "default-src 'none'"
    assert settings.server.port == 9000
    assert settings.server.drain_delay_seconds == 8.0
    assert settings.server.forwarded_allow_ips == "10.0.0.0/8"


def test_middleware_settings_default_to_off_or_safe() -> None:
    """Defaults must not turn anything on by surprise: a timeout nobody chose
    would convert working slow endpoints into 504s on upgrade, and a CSP nobody
    chose would blank out /docs."""
    settings = BaseAppSettings(_env_file=None)

    assert settings.http.timeout_seconds is None
    assert settings.http.content_security_policy is None
    assert settings.server.drain_delay_seconds == 0.0
    assert settings.server.forwarded_allow_ips is None
    assert settings.http.hsts is True  # the one that is safe to have on


# --- .env.example ---------------------------------------------------------
#
# The sample is only useful if it is true. These check it against the settings
# classes in both directions, so adding a field without documenting it, or
# leaving a key behind after removing one, fails here rather than in a
# consuming service.

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

# The field name a service is expected to give each group, which is what
# decides the env prefix. Documented at the top of .env.example.
SETTINGS_GROUPS = {
    "CORS": CorsSettings,
    "POSTGRES": DatabaseSettings,
    "REDIS": RedisSettings,
    "KEYCLOAK": KeycloakSettings,
    "OTEL": OtelSettings,
    "S3": StorageSettings,
    "PROFILER": ProfilerSettings,
    "HTTP": HttpSettings,
    "SERVER": ServerSettings,
}


# Which BaseAppSettings fields are groups rather than flat values.
SETTINGS_GROUPS_BY_FIELD = {"http", "server", "cors"}


def _documented_keys() -> dict[str, str]:
    """Every ``KEY=value`` in the sample, commented out or not.

    Prose lines start with ``# `` (hash, space); keys are written ``#KEY=`` so
    the two never collide.
    """
    keys: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        match = re.match(r"^#?([A-Z][A-Z0-9_]*)=(.*)$", line)
        if match:
            keys[match.group(1)] = match.group(2)
    return keys


def _settings_keys() -> set[str]:
    nested = set(SETTINGS_GROUPS_BY_FIELD)
    keys = {name.upper() for name in BaseAppSettings.model_fields if name not in nested}
    for prefix, model in SETTINGS_GROUPS.items():
        keys |= {f"{prefix}__{name.upper()}" for name in model.model_fields}
    return keys


def test_every_exported_settings_group_is_checked() -> None:
    """SETTINGS_GROUPS is written by hand, so it needs its own guard.

    The drift tests below only see the classes listed in it. A new group added
    to ``py_common.config`` and forgotten here would have every one of its keys
    missing from .env.example with the suite still green -- which is exactly
    the drift those tests exist to catch.
    """
    import py_common.config as config

    exported_groups = {
        getattr(config, name)
        for name in config.__all__
        if isinstance(getattr(config, name), type)
        and issubclass(getattr(config, name), BaseModel)
        and getattr(config, name) is not BaseAppSettings
    }

    assert exported_groups == set(SETTINGS_GROUPS.values())


def test_env_example_documents_every_setting() -> None:
    missing = _settings_keys() - set(_documented_keys())
    assert not missing, f".env.example is missing: {sorted(missing)}"


def test_env_example_has_no_keys_that_do_not_exist() -> None:
    extra = set(_documented_keys()) - _settings_keys()
    assert not extra, f".env.example documents settings that do not exist: {sorted(extra)}"


def _all_groups_settings() -> type[BaseAppSettings]:
    """A service that declares every optional group, so every prefix is live."""

    class Settings(BaseAppSettings):
        postgres: DatabaseSettings = DatabaseSettings()
        redis: RedisSettings = RedisSettings()
        keycloak: KeycloakSettings = KeycloakSettings()
        otel: OtelSettings = OtelSettings()
        s3: StorageSettings = StorageSettings()
        profiler: ProfilerSettings = ProfilerSettings()

    return Settings


def test_env_example_parses_when_fully_uncommented(tmp_path: Path) -> None:
    """Every value in it has to be syntactically valid.

    Lists are the trap: pydantic-settings wants JSON, and the comma-separated
    form people reach for first raises SettingsError at start-up.
    """

    Settings = _all_groups_settings()

    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(f"{k}={v}" for k, v in _documented_keys().items()))

    settings = Settings(_env_file=env_file)

    # A spot check that the values landed where the prefixes say they do.
    assert settings.postgres.pool_recycle_seconds == 1800
    assert settings.redis.url == "redis://localhost:6379/0"
    assert settings.profiler.filter_paths == ["/health", "/health/live", "/health/ready"]


def test_env_example_copied_verbatim_changes_nothing(tmp_path: Path) -> None:
    """``cp .env.example .env`` must start the service exactly as before.

    Every key in the file is live, so a copy is only safe to hand people if the
    values in it really are the defaults. Loaded as written -- comments and all
    -- it has to produce the same settings an empty file would.
    """
    settings_cls = _all_groups_settings()

    copied = tmp_path / ".env"
    copied.write_text(ENV_EXAMPLE.read_text())

    assert settings_cls(_env_file=copied).model_dump() == settings_cls(_env_file=None).model_dump()


def test_commented_keys_are_the_ones_with_no_default() -> None:
    """Only the settings whose default is "off" may stay commented out.

    No value expresses unset: an empty number fails to parse and an empty
    string is a different thing. Everything else has a default that can be
    written down, so it is written down.
    """
    commented = {
        line.lstrip("#").split("=", 1)[0]
        for line in ENV_EXAMPLE.read_text().splitlines()
        if re.match(r"^#[A-Z][A-Z0-9_]*=", line)
    }
    unset_by_default = set()
    for prefix, model in (("", BaseAppSettings), *SETTINGS_GROUPS.items()):
        for name, field in model.model_fields.items():
            if name in SETTINGS_GROUPS_BY_FIELD or field.default is not None:
                continue
            unset_by_default.add(f"{prefix}__{name.upper()}" if prefix else name.upper())

    assert commented == unset_by_default
