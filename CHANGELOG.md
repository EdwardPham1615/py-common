# Changelog

All notable changes to pycommon are recorded here. This library is shared by
multiple services, so every breaking change carries a migration note.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `KEYCLOAK__ALLOWED_AZP` — client ids allowed to have obtained the token, read
  from the `azp` claim and checked by `KeycloakTokenValidator`. Empty (the
  default) accepts any, so nothing changes until it is set. This is the check
  `verify_aud` is widely assumed to perform and does not: Keycloak fills `aud`
  from the clients the *subject holds roles on*, so any client in the realm can
  obtain a token your API accepts for any user holding a role there. A token
  with no `azp` is rejected once a list exists.

- `KEYCLOAK__TOKEN_SCOPE` — scopes to request with the `client_credentials`
  grant, sent by `ClientCredentialsTokenProvider`. Unset, no `scope` parameter
  is sent and the request is unchanged. Without it a service-account token
  carries only its client's default scopes, so no `HasScope` rule naming a
  business scope could pass for a service-to-service caller. See README's
  "Scoping a service token" for the realm configuration it needs.

- `protected_router(auth, ...)` and `internal_router(settings, ...)` build an
  `APIRouter` whose routes are authenticated as a family, nested routers
  included, and whose requirement reaches the OpenAPI document so Swagger's
  Authorize button works. Authentication belongs on the router because the only
  realistic mistake is forgetting it, which publishes an endpoint with nothing
  failing to say so.
- A composable authorization algebra — `HasRole`, `HasScope`, `Custom`, combined
  with `|` and `&` — passed to `Auth.requires(...)` on the route that needs it.
  `require_roles` and a hypothetical `require_scopes` could never between them
  express `role OR scope`; this can. A failed check answers 403 naming the rule.
- `TokenClaims.scopes` (from the standard `scope` claim) and `TokenClaims.roles`
  (realm and client roles as one set).
- `HTTP__INTERNAL_API_KEY` guards `internal_router` with an `X-API-Key` header.
  Unset installs no check, which the router logs at construction.
- `pycommon.testing.routes.assert_routes_protected(app, ...)` fails a consuming
  service's test suite, naming every operation that declares no security scheme.
  Requires the `http` extra.
- `issue_test_token(..., scopes=[...])`.

### Changed

- **BREAKING** — `create_auth_deps(validator)` is replaced by `Auth`. There is
  no shim; the import fails loudly.

  ```python
  # before
  get_current_user, require_roles = create_auth_deps(validator)

  @app.get("/me", dependencies=[Depends(get_current_user)])
  async def me(): ...

  # after
  auth = Auth(validator)
  api = protected_router(auth, prefix="/api/v1")

  @api.get("/me")
  async def me(): ...
  ```

  `auth.current_user` and `auth.require_roles(...)` behave as the old pair did,
  so a service that prefers per-route dependencies can keep them and only swap
  how they are obtained.

- `security.keycloak._unauthorized` is now public as `unauthorized`, so both
  auth paths raise the same 401.
- A 403 now names the requirement that failed
  (`Insufficient permissions; requires: role:admin`) instead of a bare
  "Insufficient permissions". This tells an authenticated caller what the policy
  is — deliberate, on the grounds that an undebuggable 403 costs more on an
  internal platform.

## [0.1.0] - 2026-09-13

First release. pycommon is the shared platform layer for internal FastAPI
services — configuration, logging, telemetry, errors, security, HTTP, cache,
runtime and persistence — so that each service writes its domain logic and not
its plumbing.

Install what you use: every subpackage maps to an optional extra
(`http`, `storage`, `security`, `telemetry`, `grpc`, `runtime`, `persistence`,
`migrations`, `cache`, `profiling`; `all` pulls everything). Only
`pydantic`, `pydantic-settings`, `structlog`, `ecs-logging`,
`opentelemetry-api`, `anyio` and `tenacity` are always installed. Python 3.13,
MIT licensed.

### Added

- **`config`** — `BaseAppSettings` plus nested settings groups for Postgres,
  Redis, Keycloak, OTel, S3 and the profiler, addressed through
  `POSTGRES__HOST`-style env keys. Environment resolution is layered and
  strict: explicit argument, then the real `ENVIRONMENT` variable, then
  `ENVIRONMENT` inside `.env`, then `dev`. An invalid value raises and names
  where it came from rather than falling back to `dev` and quietly relaxing
  security, and start-up fails outright if the env files were chosen for one
  environment while the settings claim another. `.env.example` documents all 77
  keys, with tests that keep it from drifting from the code.

- **`logging`** — ECS-shaped JSON through `structlog` + `ecs-logging`,
  correlated with OTel trace and span IDs. Third-party libraries logging
  through stdlib come out in the same shape rather than as unparseable text.
  `current_request_id()` is the single reader of the request ID, so no two
  parts of a response can disagree about it.

- **`errors`** — `ErrorCode` and `AppError` factories rendering RFC 9457
  Problem Details, including `TIMEOUT`, `PAYLOAD_TOO_LARGE` and `IDEMPOTENCY`,
  and `error_code_for_status()` for mapping an HTTP status back to a code.

- **`http`** — Problem Details handlers covering `AppError`,
  `RequestValidationError`, `HTTPException` and anything unhandled, so every
  error response is `application/problem+json` with `WWW-Authenticate` and
  `Retry-After` preserved and no internals leaked; a `/problems` documentation
  router; the `ApiResponse` envelope; offset and cursor pagination; readiness
  and liveness endpoints whose checks run concurrently; and an httpx client
  factory with a circuit-breaker transport that opens on connection failures
  and timeouts, not only on 5xx.

- **`http.middleware`** — `apply_standard_middleware` attaches the stack in the
  one order that works: CORS, security headers, RED metrics, request context
  (request ID, access log, unhandled-exception rendering), gzip, timeout, body
  limit, idempotency. Error responses keep their `X-Request-ID`, CORS and
  security headers; unhandled exceptions are logged once and still appear in
  the access log as 500s. Opt-in pieces are settings, not arguments:
  `HTTP__TIMEOUT_SECONDS`, `HTTP__MAX_BODY_BYTES`, `HTTP__GZIP_MIN_SIZE`,
  `HTTP__CONTENT_SECURITY_POLICY`, `HTTP__HSTS`. The access log and the
  rate limiter share one definition of the caller's address, which is not
  taken from a spoofable header.

- **`cache`** — a Redis client factory that sets the timeouts redis-py omits by
  default; `Cache` and the `@cached` decorator with stampede protection; a
  distributed lock with auto-extend that does not leak the lock when the
  extension fails; and fixed-window and sliding-window rate limiters with a
  `"100/minute"` DSL, `X-RateLimit-*` headers, a bounded in-memory
  implementation for dev, and Lua scripts sent once rather than per request.
  The limiter and cache fail *open* when Redis is unreachable and mark the
  response degraded, because a limiter that 500s is worse than one that lets
  traffic through.

- **`security`** — Keycloak JWT validation against JWKS, with exactly one
  refresh on an unknown `kid` (key rotation) and none on an expired or forged
  token; RBAC dependencies that answer 401 for "not authenticated" and 403 for
  "authenticated, not permitted"; and a `client_credentials` token provider
  that caches, renews before expiry, and holds a lock so a cold start does not
  stampede the identity provider.

- **`telemetry`** — OpenTelemetry traces *and* metrics over OTLP, instrumentors
  for FastAPI, httpx, Redis, SQLAlchemy and gRPC, RED metrics for both HTTP and
  gRPC, an optional Prometheus scrape endpoint, `setup_metrics` for processes
  with no FastAPI app, flush-on-shutdown, and an opt-in profiler. A missing or
  failing instrumentor never stops a service from starting.

- **`runtime`** — the FastAPI app shell, a lifespan composer, a gRPC server and
  channel pool with request-ID interceptors on all four RPC shapes, a uvicorn
  runner driven by `SERVER__*`, and connection draining on `SIGTERM` so a
  rolling deploy stops dropping in-flight requests.

- **`persistence`** — engine and sessionmaker with pool recycling ahead of
  proxy idle timeouts, structured query logging that records failures and does
  not leak bind parameters by default, `Base` with a shared naming convention,
  thin Alembic helpers (`current_revision()` returns the revision rather than
  printing it), `Repository`/`UnitOfWork`, offset and cursor pagination, and
  `UUIDv7PrimaryKeyMixin` / `TimestampMixin` / `SoftDeleteMixin`.

- **`utils`** — `retry_async`, `new_nanoid` / `new_uuid7`, `Clock` and
  `FixedClock`, and `AsyncCircuitBreaker`.

- **`testing`** — `FakeUnitOfWork`, `InMemoryRepository` and a JWT test-token
  factory, so a consuming service can test its auth and its data layer without
  standing up either.

- **`lifecycle`** — a process-wide draining flag shared by the HTTP and gRPC
  layers, so both stop accepting traffic together.

### Development

- Integration tests against real Redis, Postgres, Jaeger and MinIO, with
  `docker-compose.yaml` and `make infra-up` / `make test-integration-local`
  to run them in one command. The unit suite runs against `fakeredis` and
  `aiosqlite` and needs no services.
- CI runs lint, `mypy --strict`, the full suite against those services, and a
  dependency audit as a separate job. Coverage is gated at 85% and currently
  sits at 91%.
