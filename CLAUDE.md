# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`pycommon` is a shared platform library (config, logging, telemetry, security,
storage, HTTP helpers, runtime, persistence, cache, utils) for internal FastAPI
services, installed by pinning a git tag. It is *not* an application — there is
no domain logic here.

**Current state: `v0.1.0` shipped 2026-09-13 and no service consumes it yet.**
That is worth knowing before weighing a breaking change, and worth correcting
here the moment it stops being true — it is the difference between a rename
costing one PR and a rename costing somebody an outage. Once services do pin a
tag, a change ships to all of them at once: prefer additive changes (new
optional parameters, new modules) over breaking ones, and see "Governance" in
README.md before changing any public signature.

## Commands

```bash
make install          # uv sync --extra all --extra dev
make check            # lint + format-check + mypy --strict + test-cov — run before pushing
make lint             # ruff check src tests
make format           # ruff format (writes)
make typecheck        # mypy --strict on src/pycommon
make test             # pytest
make test-cov         # pytest with coverage (fails under 85%, see pyproject.toml)
make infra-up         # docker compose: Redis, Postgres, Jaeger, MinIO, Keycloak (waits until healthy)
make test-integration-local  # infra-up, then the integration suite with the right env
make infra-down       # stop them and delete the data
make test-integration # tests/integration when the env vars are already set — see below
make audit            # pip-audit against the locked, exported dependency set
make pre-commit       # install + run pre-commit hooks
```

`make check` runs the same lint/typecheck/test commands as CI, but **not the
same test scope**: CI's `lint-test` job sets `REDIS_TEST_URL`,
`POSTGRES_TEST_DSN`, `OTLP_TEST_ENDPOINT`, `S3_TEST_ENDPOINT` and
`KEYCLOAK_TEST_URL` at the job level, so its single `pytest` run includes the
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
make infra-up                 # Redis, Postgres, Jaeger, MinIO, Keycloak -- waits until healthy
make test-integration-local   # runs tests/integration against them
make infra-down               # stops them, deletes the data
```

`docker-compose.yaml` is the single definition of those services; the Makefile
builds the env vars from the same values. To run one group alone, set only its
variables and call `make test-integration`.

Each group reads its own variables (`REDIS_TEST_URL`, `POSTGRES_TEST_DSN`,
`OTLP_TEST_ENDPOINT`/`JAEGER_QUERY_URL`, `S3_TEST_ENDPOINT`, `KEYCLOAK_TEST_URL`)
and skips when they are unset. Point them only at throwaway instances: the Redis fixture calls
`FLUSHDB` and the Postgres one drops and recreates its tables. CI does not use
the compose file — GitHub Actions starts its own service containers — so image
tags there and in `docker-compose.yaml` have to be kept in step.

If you touch a Lua script, a TTL, the lock, the query logger, anything about
pooling, or anything in `security`, run this suite — the fake-backed unit suite proves the Python is
coherent, not that it works against the real database/broker.

The unit test suite (`make test`) runs against `fakeredis` and `aiosqlite`
instead, so it needs no running services. Know their gaps before trusting a
green run to mean more than it does: `fakeredis` doesn't faithfully execute
Lua, has no server-side `TIME`, and doesn't really expire keys; SQLite has no
statement timeout, no timezone-aware timestamp type, no
`pg_terminate_backend`, and coerces types asyncpg rejects outright. The offline
security tests are a closed loop of the same kind: the JWKS client is a mock,
the discovery document is one we wrote, and the tokens are signed by our own
keypair with the issuer and audience the validator already expects.

## Architecture

### Package/extras split

Each subpackage under `src/pycommon/` maps to an optional dependency extra in
`pyproject.toml` (`http`, `storage`, `security`, `telemetry`, `grpc`,
`runtime`, `persistence`, `migrations`, `cache`, `profiling`; `all` pulls in
everything, `dev` is tooling only). Only `pydantic`/`pydantic-settings`/
`python-dotenv`/`structlog`/`ecs-logging`/`opentelemetry-api`/`anyio`/
`tenacity` are always installed. **Import optional-dependency modules only inside the code path
that needs them** — a consumer installing `pycommon[http]` must not be forced
to have `aioboto3` or `grpcio` importable. This is the load-bearing
constraint behind the module boundaries; when adding a feature, put it in the
subpackage matching the extra it needs, don't add a new top-level dependency
without gating it behind an extra.

### Module responsibilities

| Module | Responsibility |
|--------|----------------|
| `config` | `BaseAppSettings` with `http`/`server`/`cors` built in; DB/Redis/Keycloak/OTel/S3/`ProfilerSettings` declared per service, via `POSTGRES__HOST`-style env keys. All 77 keys are written down in `.env.example`, which tests hold to the settings classes in both directions |
| `logging` | ECS JSON via `structlog` + `ecs-logging`, correlated with OTel trace/span IDs |
| `telemetry` | OTel bootstrap (traces + metrics), instrumentors, shutdown/flush, opt-in `enable_profiler` |
| `errors` | `ErrorCode` + `AppError` factories → RFC 9457 Problem Details |
| `security` | Keycloak JWT/JWKS validation, `Auth` (authn deps + `protected_router`/`internal_router`), composable authorization requirements, `client_credentials` token provider |
| `storage` | S3-compatible `ObjectStorageClient` (`aioboto3`, long-lived client) |
| `http` | Problem Details handlers + `/problems` docs, `ApiResponse` envelope, pagination, health, httpx client factory |
| `http.middleware` | Request-ID/trace context, security headers, access log, RED metrics, opt-in gzip, `apply_standard_middleware`, rate-limit dependency |
| `cache` | Redis client factory, `Cache`/`@cached` (stampede-protected), distributed lock (auto-extend), fixed/sliding-window rate limiters |
| `runtime` | FastAPI app shell, lifespan composer, gRPC server + client channel pool (request-id interceptors), uvicorn runner |
| `persistence` | Engine/sessionmaker, structured query logging, `Base` + naming convention, thin Alembic helpers, `Repository`/`UnitOfWork` |
| `utils` | `retry_async` (tenacity), `new_nanoid`/`new_uuid7`, `Clock`/`FixedClock`, `AsyncCircuitBreaker` |
| `testing` | `FakeUnitOfWork`, `InMemoryRepository`, JWT test-token factory, `assert_routes_protected` — the doubles and assertions this library ships for *consumers* to test against |
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
- **Everything configuring the HTTP surface is under `HTTP__` or `CORS__`.**
  There are no exceptions left: CORS became a group and
  `PROBLEM_TYPE_BASE_URL` moved into `HttpSettings`. The unprefixed keys are
  the genuinely app-level ones — `ENVIRONMENT`, `APP_NAME`, `APP_VERSION`,
  `DEBUG`, `LOG_LEVEL`. This matters more than tidiness: `model_config` sets
  `extra="ignore"`, so a key in the wrong shape is dropped **in silence** and
  the setting keeps its default. Check `.env.example` rather than guessing a
  spelling.
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

- **Auth is dependencies on routers, never middleware.** Middleware runs before
  routing, so its exemption list can only be path patterns — the classic bypass
  surface. An `HTTPException` raised in middleware never reaches FastAPI's
  handlers (they live inside the router), so its 401 would not be
  `problem+json`, would carry no `X-Request-ID` and would miss the access log;
  raised from a dependency it does all three. Middleware is also invisible to
  OpenAPI and cannot inject `TokenClaims` into a handler signature. All three
  were verified against this stack, not assumed.

- **Authentication attaches to the router, authorization to the route.**
  Authentication is uniform across a family of routes and the only realistic
  mistake is forgetting it, which publishes an endpoint silently — so
  `protected_router` puts it where routes inherit it, nested routers included.
  Authorization has no correct default, so it is written per endpoint and read
  in review; wrapping it into a router would breed one router per combination of
  rights. Don't add a `role_router`, and don't add a `public_router` — that is
  `APIRouter` with a different name.

- **There is no `HasPermission`, and no `KEYCLOAK__PERMISSIONS_CLAIM`.**
  Keycloak emits no permission claim by default, so both would be surface with
  nothing consuming them and the primitive would return `False` in every default
  deployment. Fine-grained rights are client roles in Keycloak
  (`HasRole("orders:write")`), and a deployment with its own mapper has
  `Custom(fn, ...)` reading `claims.raw`. Role and scope stay distinct because
  they answer different questions — who the caller is, versus what the client
  application may do on their behalf.

  That argument was originally overstated, and the integration suite is what
  showed it: `HasScope` had the same defect for any business scope, because
  `ClientCredentialsTokenProvider` sent no `scope` and a token therefore carried
  only its client's defaults. `KEYCLOAK__TOKEN_SCOPE` closed that. The
  distinction that survives is narrower than it first read — `HasScope` needs a
  standard client scope feeding the standard `scope` claim at a fixed path,
  where `HasPermission` needed a custom mapper, a non-standard claim, and a
  setting naming its path.

- **`verify_aud` proves less than its name suggests, and that is Keycloak's
  design, not our bug.** Measured against a real 26.7 server in
  `tests/integration/test_keycloak_integration.py`: Keycloak fills `aud` from
  the clients the *subject holds roles on*, not from the client that requested
  the token — that one is `azp`, which pycommon does not check. So a token
  obtained by any other client in the realm is accepted for any user holding a
  role on ours. What `verify_aud=True` does reject is a subject with no role on
  our client, who gets `aud: "account"` and a bare "Invalid or expired token".
  `.env.example` asserted the opposite until the test was written. Don't turn
  the default off to make something pass, and don't restate the old claim.

- **Bulkhead / concurrency limiting is not wanted here.** It was considered
  and dropped, not deferred: don't propose it as a gap, don't add a
  `ConcurrencyLimitMiddleware`, and don't add a semaphore primitive to `utils`
  for it. A service that needs to cap concurrent work can do it at its own
  edge or its ingress.

- **Log lines carry one name per fact.** `ecs_logging` derives `log.level` from
  the method name and supplies `@timestamp` itself, so `add_log_level` and a
  plain `timestamp` key put a second copy of both on every line. The JSON path
  therefore omits them and stamps straight into `@timestamp`; the console path
  keeps them, because `ConsoleRenderer` builds its prefix from exactly those
  two keys and loses the level entirely without them. Adding them back to the
  shared chain restores the duplication.

- **Gzip sits outside idempotency and inside metrics.** `IdempotencyMiddleware`
  stores a response and replays it for a repeated key; a compressed body in
  that store would be replayed to a client that never sent
  `Accept-Encoding: gzip`. Inside the metrics and request-context layers so
  compression time lands in the latency you alert on, and outside the timeout,
  which is a ceiling on the handler rather than on serialising its result.

- **Published tags are immutable, and the two deletions in the 0.1.0 history
  were a one-off.** `v0.2.0` was deleted and the changelog renumbered so the
  first release would be the first release, and `v0.1.0` was re-tagged once to
  include the runtime tests. Both happened while nothing consumed the library.
  The rule in RELEASING.md stands as written: from `v0.1.0` onward, a tag is
  never moved or deleted — fix a bad release by publishing the next one.

- **`CelerySettings`/`MongoSettings` were deleted, not implemented.** They
  had been exported with no module using them. Don't re-add settings ahead of
  the code that consumes them.

### Known gaps

- Not implemented, and deliberately so far unclaimed: the transactional
  outbox, feature flags, audit-log helpers, Sentry integration. For the outbox, `UnitOfWork`'s
  docstring (`persistence/unit_of_work.py:13-14`) states the limitation it
  would close — cross-engine coordination is out of scope, and callers are told
  to document that until a saga/outbox exists.
- Coverage sits at ~92% against an 85% floor. Every module is above 90% and
  eight are at 100%, so a large untested addition now stands out in the diff
  long before it reaches the floor.

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
