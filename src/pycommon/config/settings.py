"""Base settings and reusable nested settings groups.

Nested groups are plain ``BaseModel`` (not ``BaseSettings``) on purpose: a
``BaseSettings`` subclass instantiated standalone reads *bare* environment
variables (e.g. field ``user`` would pick up the shell's ``$USER``). As plain
models they are only populated through the parent settings class via the
``__`` nested delimiter (e.g. ``POSTGRES__HOST``).

Usage in a service::

    class Settings(BaseAppSettings):
        app_name: str = "my-service"
        postgres: DatabaseSettings = DatabaseSettings()
        keycloak: KeycloakSettings = KeycloakSettings()
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from pycommon.config.environment import (
    ENVIRONMENT_VAR,
    Environment,
    resolve_env_files,
    resolve_environment,
)


class HttpSettings(BaseModel):
    """Middleware knobs — env keys ``HTTP__TIMEOUT_SECONDS``, ``HTTP__HSTS``, etc.

    These are the values that differ between one deployment and the next, which
    is what makes them settings rather than arguments: a timeout that suits
    staging is wrong for production, and a CSP that suits an API is wrong for a
    service that serves its own docs. Anything structural — whether a service
    records HTTP metrics at all — stays an argument to
    :func:`~pycommon.http.middleware.apply_standard_middleware`.
    """

    # Off by default: the right ceiling depends on what the service does, and
    # one set too low turns working slow endpoints into errors. Set it below the
    # ingress timeout so the deadline that fires is the one that can explain
    # itself.
    timeout_seconds: float | None = None

    # No default value on purpose. The tight policy an API wants blanks out
    # Swagger UI and ReDoc; a policy loose enough for them protects nothing. See
    # SecurityHeadersMiddleware.
    content_security_policy: str | None = None

    hsts: bool = True
    hsts_max_age: int = 31536000

    # Off by default: a service that already accepts large uploads would start
    # rejecting them on upgrade. Set it once you know what your endpoints
    # actually take; 1 MiB suits a JSON API.
    max_body_bytes: int | None = None

    # Off by default: compression changes the bytes on the wire for every
    # response a service already serves, and the win depends on what it serves
    # -- JSON compresses well, an endpoint returning pre-compressed blobs gains
    # nothing and pays the CPU anyway. 500-1000 suits a JSON API; below roughly
    # a packet the gzip header costs more than it saves.
    gzip_min_size: int | None = None

    # How long a replayable response is kept. A day matches what clients
    # generally assume; long enough to cover a retry after an outage, short
    # enough that the store does not accumulate every write forever.
    idempotency_ttl_seconds: int = 24 * 60 * 60


class CorsSettings(BaseModel):
    """Browser CORS policy — env keys ``CORS__ORIGINS``, ``CORS__ALLOW_METHODS``, etc.

    A group rather than four flat fields, because this is configuration for the
    same middleware stack ``HTTP__*`` configures, and there is no reason for one
    to carry a prefix while the other does not.
    """

    # A frontend dev server, which is what makes this wrong everywhere else.
    origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    allow_credentials: bool = True
    allow_methods: list[str] = Field(default_factory=lambda: ["*"])
    allow_headers: list[str] = Field(default_factory=lambda: ["*"])


class ServerSettings(BaseModel):
    """Process/uvicorn knobs — env keys ``SERVER__PORT``, ``SERVER__DRAIN_DELAY_SECONDS``, etc."""

    host: str = "0.0.0.0"
    port: int = 8000

    # Peers whose X-Forwarded-* headers uvicorn should trust. Its own default is
    # 127.0.0.1, which never matches a Kubernetes ingress pod — until this is
    # set, HSTS is never emitted and every anonymous caller shares one
    # rate-limit bucket. See "Deploying behind a proxy" in the README.
    forwarded_allow_ips: str | None = None

    # Keep serving for this long after SIGTERM, with readiness failing, so load
    # balancers can take the instance out of rotation before it stops listening.
    # Must stay below terminationGracePeriodSeconds. 0 disables draining.
    drain_delay_seconds: float = 0.0


class BaseAppSettings(BaseSettings):
    """Shared settings base. Subclass in each service to add domain-specific fields.

    Env files are resolved at instantiation time: ``.env`` first, then
    ``.env.{ENVIRONMENT}`` (which takes priority). Pass ``_env_file`` explicitly
    to override.

    ``ENVIRONMENT`` is read from the process environment first and from ``.env``
    second (see :func:`~pycommon.config.environment.resolve_environment`), so
    declaring it in ``.env`` alone still selects the right environment file.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = Environment.DEV
    app_name: str = "app"
    app_version: str = "0.1.0"
    debug: bool = False
    log_level: str = "INFO"

    # Nested on the base class rather than declared per service: every HTTP
    # service using apply_standard_middleware or run_uvicorn needs them, unlike
    # postgres or keycloak which only some services have.
    http: HttpSettings = Field(default_factory=HttpSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)

    cors: CorsSettings = Field(default_factory=CorsSettings)

    # Prefix for RFC 9457 Problem Details ``type`` URIs (e.g. https://docs.example.com/problems).
    # When unset, handlers emit path-absolute types like ``/problems/input``.
    problem_type_base_url: str | None = None

    def __init__(self, **kwargs: Any) -> None:
        resolved: Environment | None = None
        if "_env_file" not in kwargs:
            resolved = resolve_environment()
            env_files = resolve_env_files(resolved)
            if env_files:
                kwargs["_env_file"] = env_files
        super().__init__(**kwargs)
        if resolved is not None and self.environment is not resolved:
            # The env files were chosen for ``resolved``, but something inside
            # them (or an explicit override) says the service is a different
            # environment. Loading .env.staging while believing you are
            # production is the failure this whole resolution order exists to
            # prevent, so it is not something to warn about and continue.
            raise ValueError(
                f"Environment mismatch: env files were resolved for "
                f"{resolved.value!r}, but the loaded settings say "
                f"{self.environment.value!r}. Set {ENVIRONMENT_VAR} consistently "
                f"in the process environment or in .env; do not override it in "
                f"an environment-specific file."
            )

    @property
    def is_dev(self) -> bool:
        return self.environment is Environment.DEV

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION


class DatabaseSettings(BaseModel):
    """Nested under a service's settings as ``postgres`` — env keys: ``POSTGRES__HOST``, etc."""

    host: str = "localhost"
    port: int = 5432
    user: str = "postgres"
    password: str = "postgres"
    db: str = "app"
    pool_size: int = 5
    max_overflow: int = 10
    pool_pre_ping: bool = True
    # Drop and reopen a pooled connection once it reaches this age. Anything
    # between the app and Postgres — pgbouncer, a cloud load balancer, a NAT
    # gateway — closes idle connections on its own schedule, often after only a
    # few minutes, without telling the pool; the next checkout then fails with
    # "server closed the connection unexpectedly" at random. Keep this *below*
    # the shortest such timeout in front of the database. ``pool_pre_ping``
    # catches the same case but pays a round-trip on every checkout, so it is a
    # safety net rather than the fix. -1 disables recycling.
    pool_recycle_seconds: int = 1800
    # Seconds to wait for a free connection before raising, instead of blocking
    # a request forever behind an exhausted pool.
    pool_timeout_seconds: float = 30.0
    echo: bool = False
    echo_pool: bool = False
    # Structured query logging (SQLAlchemy event listeners → structlog)
    log_queries: bool = False
    slow_query_threshold_ms: float = 0.0  # 0 = log every query at debug; >0 = warn when slower
    log_query_params: bool = False  # off by default to avoid PII / secrets in logs
    # Opt-in Alembic upgrade on app startup (keep False in production)
    auto_migrate: bool = False
    migrations_script_location: str = "alembic"

    def _dsn(self, driver: str) -> str:
        # quote(), not quote_plus(): the latter is for query strings, where a
        # space means "+". In the userinfo part of a URL a "+" is a literal plus,
        # and no URL parser turns it back into a space -- so a password with a
        # space in it authenticated as something else entirely and the service
        # simply could not connect. safe="" so "/" and ":" are escaped too,
        # since either would otherwise end the userinfo early.
        auth = f"{quote(self.user, safe='')}:{quote(self.password, safe='')}"
        return f"postgresql+{driver}://{auth}@{self.host}:{self.port}/{self.db}"

    @property
    def async_dsn(self) -> str:
        return self._dsn("asyncpg")

    @property
    def sync_dsn(self) -> str:
        return self._dsn("psycopg")


class RedisSettings(BaseModel):
    url: str = "redis://localhost:6379/0"
    max_connections: int = 10
    lock_timeout_seconds: int = 30
    # redis-py blocks forever without these; a hung connection would pin a task.
    socket_timeout_seconds: float = 5.0
    socket_connect_timeout_seconds: float = 5.0
    # Ping idle connections before reuse — a load balancer may have dropped them.
    health_check_interval_seconds: int = 30
    retry_on_timeout: bool = True


class KeycloakSettings(BaseModel):
    server_url: str = "http://localhost:8080"
    realm: str = "app"
    client_id: str = "app-api"
    client_secret: str = ""
    audience: str | None = None
    algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    jwks_cache_ttl_seconds: int = 3600
    verify_aud: bool = True

    @property
    def issuer(self) -> str:
        return f"{self.server_url.rstrip('/')}/realms/{self.realm}"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/certs"

    @property
    def token_url(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/token"

    @property
    def openid_config_url(self) -> str:
        return f"{self.issuer}/.well-known/openid-configuration"


class OtelSettings(BaseModel):
    enabled: bool = True
    service_name: str = "app"
    exporter_otlp_endpoint: str = "http://localhost:4317"
    exporter_otlp_insecure: bool = True
    traces_sampler_arg: float = 1.0

    # Metrics ride the same OTLP endpoint as traces. Kept separately switchable
    # because sampling traces down is normal, while metrics must stay complete
    # for a rate or error ratio to mean anything.
    metrics_enabled: bool = True
    metrics_export_interval_ms: int = 60_000
    prometheus_enabled: bool = False
    prometheus_path: str = "/metrics"


class StorageSettings(BaseModel):
    endpoint_url: str = "http://localhost:8333"
    access_key: str = "any"
    secret_key: str = "any"
    region: str = "us-east-1"
    bucket: str = "app"
    use_path_style: bool = True
    public_base_url: str | None = None


class ProfilerSettings(BaseModel):
    """Opt-in pyinstrument profiler (requires the ``profiling`` extra).

    Defaults are safe for production: disabled, low sample rate, only profile
    slow requests / 5xx, dashboard off.
    """

    enabled: bool = False
    sample_rate: float = 0.1
    slow_request_threshold_ms: float = 200.0
    always_profile_errors: bool = True
    enable_dashboard: bool = False
    dashboard_path: str = "/__profiler__"
    allow_dashboard_in_production: bool = False
    filter_paths: list[str] = Field(
        default_factory=lambda: ["/health", "/health/live", "/health/ready"]
    )
