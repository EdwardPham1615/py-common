# Changelog

All notable changes to py-common are recorded here. This library is shared by
multiple services, so every breaking change carries a migration note.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- The access log can name the caller. `RequestContextMiddleware` has always read
  `request.state.user.sub` into the log's `user.id` field, but nothing in the
  library wrote that field — it was documented and never populated, so a service
  either added its own dependency to bind the claims or accepted an access log
  that could not say who did anything.

  `Auth.current_user` and `Auth.optional_user` now publish the decoded claims on
  `request.state.user`. Additive, and it costs no extra token decode: the closures
  are still built once, so FastAPI's per-callable dependency cache still resolves
  them a single time per request.

  `optional_user` writes the field only when a token was actually presented, so an
  anonymous request leaves `user.id` absent rather than present and empty.


- `setup_logging` now adopts uvicorn's own loggers, so the log format holds
  however the process was started. Under the `uvicorn` CLI, uvicorn applies its
  `LOGGING_CONFIG` via `dictConfig` **before importing the app**, giving the
  `uvicorn` logger its own handler and `propagate: False` — so replacing the root
  handler never reached it, and uvicorn's startup and shutdown lines stayed plain
  text in an otherwise ECS stream. A shipper parsing one JSON object per line
  drops exactly the lines you want during an incident.

  `run_uvicorn` already avoided this by passing `log_config=None`; nothing stopped
  a consumer from using the CLI.

  `uvicorn.access` is deliberately **not** adopted. `RequestContextMiddleware`
  already emits the access log, so letting uvicorn's through as well would put two
  access lines on every request; it stays silenced by level.

### Changed

- **BREAKING** — `persistence` now declares `sqlalchemy[asyncio]>=2.0.52,<2.1`
  instead of an open-ended floor. A service already resolving 2.1.x cannot install
  this version until the ceiling moves.

  The ceiling exists because of the *instrumentor*, not SQLAlchemy:
  `opentelemetry-instrumentation-sqlalchemy` declares
  `sqlalchemy >= 1.0.0, < 2.1.0` — still true of 0.66b0, the newest release as of
  2026-09-27 — and against 2.1 it refuses to instrument, logging one error at
  startup and then producing **no database spans at all**. So 2.1 was never
  working; it was failing silently. Being unable to install is the louder of the
  two.

  Measured: with the ceiling a fresh resolve gives 2.0.54, without it 2.1.1.

### Fixed

- `assert_routes_protected` counted a route with *optional* authentication as
  protected. A route depending on `Auth.optional_user` lets an anonymous caller
  through, but it declared the same `HTTPBearer` scheme as `Auth.current_user`,
  so the generated document was identical and the audit reported success on an
  endpoint anyone could call — the one thing that helper exists not to do.

  `Auth.optional_user` now declares its own scheme, exported as
  `py_common.security.OPTIONAL_AUTH_SCHEME`, and the check treats a route whose
  only requirement is that scheme as open. Nothing about who may call such a route
  changed; both dependencies still read the same header.

  **This can newly fail a consumer's suite**, which is the point: list
  optional-auth routes in `public_paths`/`public_prefixes`. A route that is on a
  `protected_router` *as well* stays protected — the router's guard demands a
  token, and the check asks whether any declared requirement does.

  The OpenAPI document also gains an `OptionalBearer` security scheme, visible as
  a second entry in Swagger's Authorize dialog.


- `py-common[migrations]` could not import `py_common.persistence.migrations`. It
  failed with *"The SQLAlchemy asyncio module requires that the Python 'greenlet'
  library is installed"* — because importing that submodule runs
  `persistence/__init__.py`, which imported `engine` and therefore
  `sqlalchemy.ext.asyncio`, whose `greenlet` dependency only the `persistence`
  extra's `sqlalchemy[asyncio]` brings. The Alembic helpers are synchronous and
  need none of it.

  `persistence/__init__.py` now resolves its re-exports on first access, like
  `runtime` and `http` since 0.2.1. No public name moved.

  It went unnoticed because whether it fails depends on what a fresh resolve
  happens to pull in; CI stayed green while a local run went red.
  `scripts/check-extras-isolation.sh` resolves fresh, which is how it surfaced.

- `scripts/check-extras-isolation.sh` now also imports `py_common.testing.routes`
  (under `http`) and `py_common.testing.tokens` (under `security`). They are
  public API a consumer imports in its own test suite and each carries its own
  extra, so leaving them out left a hole exactly where a helper is most likely to
  reach across packages.


## [0.2.1] - 2026-09-18

### Fixed

- Four extras could not import the modules they promise. Installing exactly
  what the README documents was enough to hit it:

  | install | import | 0.2.0 |
  |---|---|---|
  | `py-common[http]` | `py_common.http.middleware` | `No module named 'redis'` |
  | `py-common[runtime]` | `py_common.runtime` | `No module named 'opentelemetry.exporter'` |
  | `py-common[persistence]` | `py_common.persistence` | `No module named 'fastapi'` |
  | `py-common[persistence]` | `py_common.persistence.migrations` | `No module named 'httpx'` |

  The `http` one took out `apply_standard_middleware`, which is the entry point
  that module exists for and the first thing README's "Quick usage" calls.

  The cause was the same in every case: a package `__init__` importing all of
  its submodules eagerly. Python executes that file whenever anything imports
  *any* submodule, so the package's whole dependency set became every
  importer's dependency set — `py_common.persistence.pagination` reaching for a
  cursor helper was paying for `httpx`, and the HTTP metrics middleware was
  paying for the OTel SDK.

  `py_common.runtime` and `py_common.http` now resolve their re-exports on first
  access (PEP 562), `py_common.http.middleware` defers only the `idempotency`
  names, and `setup_telemetry` imports the OTel SDK in its own body the way it
  already did for the instrumentors. Every public name is reachable exactly as
  before; `from py_common.runtime import create_base_app` is unchanged.

  No API changed, so there is nothing to migrate — but a service that worked
  around this by installing extras it does not use can stop.

- `scripts/check-extras-isolation.sh` installs each extra alone and imports what
  it promises, wired into CI as its own job. The unit suite runs under
  `--extra all`, so it structurally cannot catch this class of bug: every
  optional dependency is present and a module reaching outside its extra looks
  fine.

## [0.2.0] - 2026-09-18

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

- `py_common.testing.routes.assert_routes_protected(app, ...)` fails a consuming
  service's test suite, naming every operation that declares no security scheme.
  Requires the `http` extra.

- `issue_test_token(..., scopes=[...])`.

### Changed

- **BREAKING** — the distribution is renamed `pycommon` → **`py-common`**, and
  the import package `pycommon` → **`py_common`**.

  ```python
  # before
  from pycommon.security import Auth

  # after
  from py_common.security import Auth
  ```

  ```toml
  # before
  dependencies = ["pycommon[all] @ git+https://github.com/EdwardPham1615/pycommon.git@v0.1.0"]

  # after
  dependencies = ["py-common[all] @ git+https://github.com/EdwardPham1615/py-common.git@v0.2.0"]
  ```

  The repository moved with it, from `pycommon` to `py-common`. GitHub redirects
  the old path, so an existing clone or link keeps working — but the URL above is
  the one to write down, and the wheel now carries it in `Project-URL` metadata
  so a built artifact can be traced back to this repository rather than to the
  unrelated `pycommon` on PyPI. Note the two spellings: `py-common` wherever a distribution is named (install commands,
  extras, error messages telling you what to install), `py_common` wherever
  Python is — a hyphen is not a valid identifier.

  Why: `pycommon` is taken on PyPI by an unrelated project, so `pip install
  pycommon` fetches a stranger's package, and tooling reported spurious updates
  for it. `py-common` and `py_common` are both unclaimed.

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

- **BREAKING** — `requires-python` is now `>=3.14`, up from `>=3.13`. A service
  on 3.13 cannot install this version; upgrade the service's interpreter, or
  stay on a tag published before this change.

  The floor now tracks the current stable release rather than the oldest one
  still receiving fixes. Python 3.13 left its bugfix window on 2026-10-01 and is
  security-only until 2029; 3.14 has bugfix support until 2027-10. Nothing in
  the library needed 3.14 — the whole suite, including the integration group,
  passes identically on both — so this is a support commitment, not a feature
  gate, and it is being made now because raising a floor is cheap while nothing
  consumes the library and expensive once services pin tags.

  Python 3.15 was evaluated and is not usable yet: `psycopg-binary` publishes no
  `cp315` wheel, and `asyncpg`, `uvloop`, `httptools` and `lupa` have none either
  — they install only by compiling from source, which a slim container image
  cannot do. Worth revisiting once those ship.

- `ruff` now targets `py314`, which reformats `except (A, B):` to `except A, B:`
  (PEP 758). One occurrence today, in `cache/cached.py`.

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
