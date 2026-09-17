"""Smoke tests: every public submodule must be importable when extras are installed."""

from __future__ import annotations


def test_import_package() -> None:
    """``__version__`` comes from installed metadata, so it cannot drift from
    ``pyproject.toml`` — and this assertion does not need editing every release."""
    from importlib.metadata import version

    import py_common

    assert py_common.__version__ == version("py_common")
    assert py_common.__version__ != "0.0.0+unknown"


def test_import_config() -> None:
    from py_common.config import BaseAppSettings, Environment

    assert Environment.DEV == "dev"
    assert BaseAppSettings is not None


def test_import_logging() -> None:
    from py_common.logging import get_logger, setup_logging

    assert callable(setup_logging)
    assert callable(get_logger)


def test_import_telemetry() -> None:
    from py_common.telemetry import (
        enable_profiler,
        instrument_sqlalchemy,
        setup_telemetry,
        shutdown_telemetry,
    )

    assert callable(setup_telemetry)
    assert callable(shutdown_telemetry)
    assert callable(instrument_sqlalchemy)
    assert callable(enable_profiler)


def test_import_security() -> None:
    from py_common.security import (
        Auth,
        ClientCredentialsTokenProvider,
        HasRole,
        HasScope,
        KeycloakTokenValidator,
        TokenClaims,
        internal_router,
        protected_router,
    )

    assert TokenClaims is not None
    assert KeycloakTokenValidator is not None
    assert ClientCredentialsTokenProvider is not None
    assert Auth is not None
    assert callable(protected_router)
    assert callable(internal_router)
    assert (HasRole("a") | HasScope("b")).describe() == "role:a OR scope:b"


def test_import_storage() -> None:
    from py_common.storage import ObjectStorageClient

    assert ObjectStorageClient is not None


def test_import_errors() -> None:
    from py_common.errors import AppError, ErrorCode, problem_type_uri

    assert AppError.input().error_code is ErrorCode.INPUT
    assert problem_type_uri(ErrorCode.INPUT) == "/problems/input"


def test_import_http() -> None:
    from py_common.http import (
        ApiResponse,
        HealthCheck,
        Page,
        ProblemDetail,
        build_health_router,
        build_problem_types_router,
        create_http_client,
        problem_response,
        register_exception_handlers,
    )

    assert ProblemDetail is not None
    assert ApiResponse is not None
    assert Page is not None
    assert HealthCheck is not None
    assert callable(problem_response)
    assert callable(build_health_router)
    assert callable(build_problem_types_router)
    assert callable(create_http_client)
    assert callable(register_exception_handlers)


def test_import_http_middleware() -> None:
    from py_common.http.middleware import (
        RequestContextMiddleware,
        SecurityHeadersMiddleware,
        apply_standard_middleware,
    )
    from py_common.http.middleware.rate_limit import build_rate_limit_dep

    assert RequestContextMiddleware is not None
    assert SecurityHeadersMiddleware is not None
    assert callable(apply_standard_middleware)
    assert callable(build_rate_limit_dep)


def test_import_cache() -> None:
    from py_common.cache import (
        InMemoryRateLimiter,
        RedisRateLimiter,
        create_redis,
        redis_lock,
    )

    assert callable(create_redis)
    assert callable(redis_lock)
    assert InMemoryRateLimiter is not None
    assert RedisRateLimiter is not None


def test_import_runtime() -> None:
    from py_common.runtime import (
        GrpcChannelPool,
        GrpcServer,
        LifespanResource,
        RequestIdClientInterceptor,
        RequestIdServerInterceptor,
        build_lifespan,
        create_base_app,
        run_uvicorn,
    )

    assert callable(create_base_app)
    assert callable(build_lifespan)
    assert LifespanResource is not None
    assert GrpcServer is not None
    assert GrpcChannelPool is not None
    assert RequestIdServerInterceptor is not None
    assert RequestIdClientInterceptor is not None
    assert callable(run_uvicorn)


def test_import_persistence() -> None:
    from py_common.config import DatabaseSettings
    from py_common.persistence import (
        Base,
        Repository,
        SqlAlchemyRepository,
        SqlAlchemyUnitOfWork,
        UnitOfWork,
        build_alembic_config,
        create_engine_and_sessionmaker,
        install_query_logger,
        metadata,
        migration_lifespan_resource,
    )

    assert Repository is not None
    assert SqlAlchemyRepository is not None
    assert UnitOfWork is not None
    assert SqlAlchemyUnitOfWork is not None
    assert callable(create_engine_and_sessionmaker)
    assert callable(install_query_logger)
    assert callable(migration_lifespan_resource)
    assert "pk" in metadata.naming_convention
    assert issubclass(Base, object)

    settings = DatabaseSettings(host="db.example", db="orders")
    config = build_alembic_config(settings, script_location="alembic")
    assert config.get_main_option("sqlalchemy.url") == settings.sync_dsn
    assert config.get_main_option("script_location") == "alembic"


def test_import_utils() -> None:
    from py_common.utils import (
        AsyncCircuitBreaker,
        FixedClock,
        new_nanoid,
        new_uuid7,
        retry_async,
        standard_retry,
        utcnow,
    )

    assert callable(retry_async)
    assert callable(standard_retry)
    assert callable(new_nanoid)
    assert callable(new_uuid7)
    assert AsyncCircuitBreaker is not None
    assert FixedClock is not None
    assert utcnow().tzinfo is not None


def test_import_config_profiler_settings() -> None:
    from py_common.config import ProfilerSettings

    assert ProfilerSettings().enabled is False


def test_import_testing() -> None:
    from py_common.testing.fakes import FakeUnitOfWork, InMemoryRepository
    from py_common.testing.tokens import generate_rsa_keypair, issue_test_token

    assert FakeUnitOfWork is not None
    assert InMemoryRepository is not None
    assert callable(generate_rsa_keypair)
    assert callable(issue_test_token)


def test_error_code_registry_covers_new_statuses() -> None:
    """Every non-OK ErrorCode must be resolvable to a problem type and URI."""
    from py_common.errors import PROBLEM_TYPES, ErrorCode, problem_type_uri

    for code in ErrorCode:
        if code is ErrorCode.OK:
            continue
        assert code in PROBLEM_TYPES, f"{code!r} has no ProblemType"
        assert problem_type_uri(code).startswith("/problems/")

    slugs = [p.slug for p in PROBLEM_TYPES.values()]
    assert len(slugs) == len(set(slugs)), "problem type slugs must be unique"


def test_error_code_for_status_mapping() -> None:
    from py_common.errors import ErrorCode, error_code_for_status

    assert error_code_for_status(401) is ErrorCode.AUTH
    # 403 resolves to FORBIDDEN, not APP_CHECK, even though both are 403.
    assert error_code_for_status(403) is ErrorCode.FORBIDDEN
    assert error_code_for_status(429) is ErrorCode.RATE_LIMIT
    assert error_code_for_status(422) is ErrorCode.INPUT
    assert error_code_for_status(503) is ErrorCode.SERVER
    # No application meaning — callers must omit error_code rather than guess.
    assert error_code_for_status(418) is None


def test_app_error_factories_use_registry_status() -> None:
    from py_common.errors import PROBLEM_TYPES, AppError

    for factory, code in (
        (AppError.forbidden, "FORBIDDEN"),
        (AppError.not_found, "NOT_FOUND"),
        (AppError.conflict, "CONFLICT"),
        (AppError.rate_limit, "RATE_LIMIT"),
    ):
        err = factory("boom")
        assert err.error_code.name == code
        assert err.status_code == PROBLEM_TYPES[err.error_code].status_code
        assert err.detail == "boom"
