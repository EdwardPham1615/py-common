# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`pycommon` is a shared platform library (config, logging, telemetry, security,
storage, HTTP helpers, runtime, persistence, cache, utils) consumed by
multiple internal FastAPI services via a pinned git tag. It is *not* an
application — there is no domain logic here, and a change ships to every
consumer at once. Keep that in mind for anything beyond a pure bugfix: prefer
additive changes (new optional parameters, new modules) over breaking ones,
and see "Governance" in README.md before changing any public signature.

## Commands

```bash
make install          # uv sync --extra all --extra dev
make check            # lint + format-check + mypy --strict + test-cov — run before pushing
make lint             # ruff check src tests
make format           # ruff format (writes)
make typecheck        # mypy --strict on src/pycommon
make test             # pytest
make test-cov         # pytest with coverage (fails under 85%, see pyproject.toml)
make test-integration # tests/integration against real Redis/Postgres — see below
make audit            # pip-audit against the locked, exported dependency set
make pre-commit       # install + run pre-commit hooks
```

`make check` runs the same lint/typecheck/test commands as CI, but **not the
same test scope**: CI's `lint-test` job sets `REDIS_TEST_URL`,
`POSTGRES_TEST_DSN`, `OTLP_TEST_ENDPOINT` and `S3_TEST_ENDPOINT` at the job
level against service containers, so its single `pytest` run includes the
integration suite that skips locally. A green `make check` can still meet a
red CI, and local coverage reads lower than CI's for the same reason.

Single test / single file (tests are flat in `tests/`, one file per area):

```bash
uv run pytest tests/test_cache.py::test_name
uv run pytest tests/test_persistence.py -k pattern
```

`asyncio_mode = "auto"` is set, so async tests need no `@pytest.mark.asyncio`
decorator. Integration test modules carry
`pytestmark = pytest.mark.integration`, and the per-service *skip* lives in
each fixture in `tests/integration/conftest.py` — not in a module-level
`pytestmark` there, which pytest silently ignores in a `conftest.py`, letting
the tests run against a null URL and fail instead of skipping. Keep new
integration fixtures on that pattern.

### Integration tests

`tests/integration` needs real services and skips per-group when its env var
is unset — don't expect `make test-integration` to do anything without them:

```bash
docker run -d --rm -p 6379:6379 redis:7-alpine
docker run -d --rm -p 5432:5432 \
  -e POSTGRES_USER=pycommon -e POSTGRES_PASSWORD=pycommon -e POSTGRES_DB=pycommon_test \
  -e POSTGRES_INITDB_ARGS="--auth-host=scram-sha-256 --auth-local=scram-sha-256" \
  postgres:17-alpine

REDIS_TEST_URL=redis://localhost:6379/15 \
POSTGRES_TEST_DSN=postgresql+asyncpg://pycommon:pycommon@localhost:5432/pycommon_test \
  make test-integration
```

Telemetry and object-storage groups need their own containers
(`OTLP_TEST_ENDPOINT`/`JAEGER_QUERY_URL` for Jaeger, `S3_TEST_ENDPOINT` for
MinIO) — see the Testing section of CONTRIBUTING.md for the exact commands.
Point these only at throwaway instances: the Redis fixture calls `FLUSHDB`
and the Postgres one drops and recreates its tables. If you touch a Lua
script, a TTL, the lock, the query logger, or anything about pooling, run
this suite — the fake-backed unit suite proves the Python is coherent, not
that it works against the real database/broker.

The unit test suite (`make test`) runs against `fakeredis` and `aiosqlite`
instead, so it needs no running services. Know their gaps before trusting a
green run to mean more than it does: `fakeredis` doesn't faithfully execute
Lua, has no server-side `TIME`, and doesn't really expire keys; SQLite has no
statement timeout, no timezone-aware timestamp type, no
`pg_terminate_backend`, and coerces types asyncpg rejects outright.

## Architecture

### Package/extras split

Each subpackage under `src/pycommon/` maps to an optional dependency extra in
`pyproject.toml` (`http`, `storage`, `security`, `telemetry`, `grpc`,
`runtime`, `persistence`, `migrations`, `cache`, `profiling`; `all` pulls in
everything, `dev` is tooling only). Only `pydantic`/`pydantic-settings`/
`structlog`/`ecs-logging`/`opentelemetry-api`/`anyio`/`tenacity` are always
installed. **Import optional-dependency modules only inside the code path
that needs them** — a consumer installing `pycommon[http]` must not be forced
to have `aioboto3` or `grpcio` importable. This is the load-bearing
constraint behind the module boundaries; when adding a feature, put it in the
subpackage matching the extra it needs, don't add a new top-level dependency
without gating it behind an extra.

### Module responsibilities

| Module | Responsibility |
|--------|----------------|
| `config` | `BaseAppSettings`; nested DB/Redis/Keycloak/OTel/S3/`ProfilerSettings` via `POSTGRES__HOST`-style env keys |
| `logging` | ECS JSON via `structlog` + `ecs-logging`, correlated with OTel trace/span IDs |
| `telemetry` | OTel bootstrap (traces + metrics), instrumentors, shutdown/flush, opt-in `enable_profiler` |
| `errors` | `ErrorCode` + `AppError` factories → RFC 9457 Problem Details |
| `security` | Keycloak JWT/JWKS validation, RBAC deps, `client_credentials` token provider |
| `storage` | S3-compatible `ObjectStorageClient` (`aioboto3`, long-lived client) |
| `http` | Problem Details handlers + `/problems` docs, `ApiResponse` envelope, pagination, health, httpx client factory |
| `http.middleware` | Request-ID/trace context, security headers, access log, RED metrics, `apply_standard_middleware`, rate-limit dependency |
| `cache` | Redis client factory, `Cache`/`@cached` (stampede-protected), distributed lock (auto-extend), fixed/sliding-window rate limiters |
| `runtime` | FastAPI app shell, lifespan composer, gRPC server + client channel pool (request-id interceptors), uvicorn runner |
| `persistence` | Engine/sessionmaker, structured query logging, `Base` + naming convention, thin Alembic helpers, `Repository`/`UnitOfWork` |
| `utils` | `retry_async` (tenacity), `new_nanoid`/`new_uuid7`, `Clock`/`FixedClock`, `AsyncCircuitBreaker` |
| `testing` | `FakeUnitOfWork`, `InMemoryRepository`, JWT test-token factory — the in-memory doubles this library ships for *consumers* to test against |
| `lifecycle` (top-level module) | Process-wide draining flag (`begin_draining`/`is_draining`/`reset_draining`), shared by the HTTP and gRPC layers so both stop accepting traffic together. The only thing `pycommon/__init__.py` re-exports besides `__version__`. |

Read README.md's "Quick usage" and per-topic sections
(Idempotency keys, Request body limits, Request timeouts, Graceful
shutdown/draining, Pagination, ORM mixins) before touching those areas —
each documents a specific failure mode the current design avoids, not just
the API shape.

### Cross-cutting design decisions that shape the code

These recur across modules and explain choices that otherwise look
inconsistent:

- **Environment resolution is layered and strict.** `resolve_environment()`
  checks (in order) an explicit argument, the real `ENVIRONMENT` process env
  var, `ENVIRONMENT` inside `.env`, then defaults to `dev`. `.env` loads
  first, then `.env.{environment}` on top. An invalid value raises and names
  its source rather than silently falling back to `dev`. `get_environment()`
  and `settings.environment` must always resolve identically.
- **Fail open on infrastructure, fail closed on authorization.** The rate
  limiter and cache degrade gracefully (and mark the response as degraded in
  metrics) if Redis is unreachable. Idempotency keys are the deliberate
  exception: they fail closed (503) because a duplicate write is worse than a
  rejected one — see README's "Idempotency keys".
- **Errors are Problem Details everywhere, uniformly.**
  `register_exception_handlers` converts `AppError`, FastAPI's
  `RequestValidationError`, `HTTPException`, and any unhandled exception into
  `application/problem+json`, preserving headers like `WWW-Authenticate` and
  `Retry-After` and never leaking internals for the unhandled case.
  `RequestContextMiddleware` must sit inside the app's CORS/security layers
  (via `apply_standard_middleware`) so that even a 500 raised in Starlette's
  outer `ServerErrorMiddleware` still carries `X-Request-ID` and CORS
  headers.
- **Settings, not function arguments, for anything environment-dependent**
  (middleware toggles, pool sizing, body limits) — an operator changes
  behavior via env vars without a code release.
- **Bound anything keyed by caller-controlled input** (IPs, paths, header
  values) to avoid unbounded dicts/metric-label cardinality.
- **`SqlAlchemyRepository.delete` issues a bulk `DELETE`** — it does not walk
  ORM cascades or fire `before_delete`/`after_delete`. Express deletes as
  DB-level `ON DELETE CASCADE`, or override `delete()`.
- **When you extend an interface in a real module, extend its fake in
  `pycommon.testing.fakes` in the same PR** — otherwise the fake silently
  stops being a faithful substitute and consumer test suites lose coverage
  without any test failing.

## Settled decisions — don't re-litigate

An audit in 2026-08 drove most of the current design. Its findings are now
code, so treat these as closed unless something new contradicts them:

- **The error path was rebuilt deliberately.** 500s losing CORS/`X-Request-ID`,
  422/401/403/429 not being problem+json, and 500s being double-logged while
  missing from the access log were all real, reproduced bugs. The current
  shape — `RequestContextMiddleware(handle_exceptions=True)` rendering the
  problem response from inside the middleware stack, with the `Exception`
  handler kept only as an outer safety net — is the fix, not an accident.
  Changing it regresses three bugs at once.
- **The circuit breaker wraps a transport, not response event hooks.** Hooks
  only fire when a response exists, so `ConnectError`/timeout — the case the
  breaker exists for — never tripped it. Keep failure counting in
  `CircuitBreakerTransport.handle_async_request`, and use
  `AsyncCircuitBreaker`'s public `before_call`/`on_success`/`on_failure`
  rather than private methods.
- **`fastapi-redis-sdk` and `fastapi-guard` were evaluated and rejected**
  for this library (not for services). Reasons are written up in README's
  "Libraries we deliberately don't vendor"; the ideas worth taking from the
  Redis SDK — fail-open with a `degraded` flag, the `"100/minute"` rate DSL,
  `X-RateLimit-*` headers — are already implemented in `pycommon.cache`.
- **The gRPC request-ID interceptor wraps the handler behavior, not the
  lookup.** `intercept_service` only resolves a handler and returns before it
  runs, and the old `bind_contextvars` there was never unwound, so a request ID
  outlived its RPC. `RequestIdServerInterceptor` now `_replace`s the behavior
  (all four shapes, via `runtime/_grpc_behavior.py`, shared with
  `MetricsServerInterceptor`) and holds `bound_contextvars` for exactly the
  handler's lifetime. Binding in `intercept_service` again reintroduces the
  leak.

- **`CelerySettings`/`MongoSettings` were deleted, not implemented.** They
  had been exported with no module using them. Don't re-add settings ahead of
  the code that consumes them.

### Known gaps

- Not implemented, and deliberately so far unclaimed: GZip/compression
  middleware, the transactional outbox (there is a TODO pointing at it in
  `persistence/unit_of_work.py:14`), bulkhead/concurrency limiting, feature
  flags, audit-log helpers, Sentry integration.
- Coverage sits around 87% against an 85% floor, so there is roughly two
  points of headroom — a sizeable untested addition will fail `make test-cov`
  on the floor, not just look untidy.

## Repo conventions (from CONTRIBUTING.md)

- Branch names: `<type>/<short-slug>` where type is `feat`, `fix`, `docs`,
  `refactor`, `test`, `perf`, or `chore`.
- Commit subject in imperative mood, describing what the change does for the
  system, not which files changed; body explains *why*, wrapped at 72 cols.
- Update `CHANGELOG.md` under `## [Unreleased]` for anything consumer-visible
  (skip for internal refactors/test-only changes); mark breaking changes
  **BREAKING** with a before/after migration note.
- PRs are squash-merged by default (rebase only when every commit in the
  branch already stands on its own and passes CI); `main` stays linear.
