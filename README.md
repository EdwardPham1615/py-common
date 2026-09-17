# pycommon

Reusable platform library for FastAPI (and related) Python services: config, logging, telemetry, security, storage, HTTP helpers, runtime, persistence, cache, and shared utilities.

## Install

**Requires Python 3.14+.** The floor tracks the current stable release rather
than the oldest still receiving security fixes: a consuming service is one this
team also runs, and 3.13 left its bugfix window on 2026-10-01.

Note the name: there is an unrelated `pycommon` on PyPI. Install from the Git
URL below, never `pip install pycommon`.

### From a Git URL (recommended for consumers)

```bash
# uv
uv add "pycommon[all] @ git+https://github.com/EdwardPham1615/pycommon.git@v0.1.0"

# pip
pip install "pycommon[all] @ git+https://github.com/EdwardPham1615/pycommon.git@v0.1.0"
```

Pin a tag or commit SHA for reproducible builds.

### Local editable (monorepo co-development)

```toml
# in your service pyproject.toml
dependencies = ["pycommon[all]"]

[tool.uv.sources]
pycommon = { path = "../pycommon", editable = true }
```

### Optional extras

Core always installs: `pydantic`, `pydantic-settings`, `structlog`, `ecs-logging`, `opentelemetry-api`, `anyio`, `tenacity`.

| Extra | Pulls in |
|-------|----------|
| `http` | FastAPI / Starlette / httpx (Problem Details, pagination, health, middleware, client factory) |
| `storage` | aioboto3 (S3-compatible object storage) |
| `security` | FastAPI + httpx + PyJWT (Keycloak JWT / RBAC, service tokens) |
| `telemetry` | OpenTelemetry SDK + exporters + instrumentors |
| `grpc` | grpcio + OTel gRPC instrumentation |
| `runtime` | FastAPI + uvicorn + multipart + grpcio |
| `persistence` | SQLAlchemy asyncio |
| `migrations` | Alembic (thin helpers; versions stay in the service) |
| `cache` | redis (client factory, distributed lock, rate limiting) |
| `profiling` | fastapi_profiler / pyinstrument (opt-in request profiler) |
| `all` | Everything above |
| `dev` | ruff, mypy, pytest, pre-commit, aiosqlite, fakeredis |

Example: `uv add "pycommon[http,persistence,runtime] @ git+https://github.com/EdwardPham1615/pycommon.git@v0.1.0"`

## Modules

| Module | Responsibility |
|--------|----------------|
| `config` | `BaseAppSettings`, nested DB/Redis/Keycloak/OTel/S3/`ProfilerSettings` (via `POSTGRES__HOST`-style env keys) |
| `logging` | ECS JSON via `structlog` + `ecs-logging` + OTel correlation |
| `telemetry` | OpenTelemetry bootstrap (traces + metrics) + instrumentors + shutdown/flush + opt-in `enable_profiler` |
| `errors` | `ErrorCode` + `AppError` factories → RFC 9457 Problem Details with `type` URI + `error_code` |
| `security` | Keycloak JWT/JWKS validation, `Auth` deps + router factories, composable authorization requirements, `client_credentials` token provider |
| `storage` | S3-compatible `ObjectStorageClient` (`aioboto3`, long-lived client) |
| `http` | Problem Details + handlers + `/problems` docs, `ApiResponse` envelope, pagination, health, httpx client |
| `http.middleware` | Request-ID/trace context, security headers, access log, RED metrics, `apply_standard_middleware`, rate-limit dependency |
| `cache` | Redis factory, value cache (`Cache` / `@cached`, stampede-protected), distributed lock (auto-extend), fixed- and sliding-window rate limiters |
| `runtime` | FastAPI shell, lifespan composer, gRPC server + client channel pool (request-id interceptors), uvicorn runner |
| `persistence` | Engine/sessionmaker, structured query logging, `Base` + naming convention, Alembic helpers, `Repository` / `UnitOfWork` |
| `utils` | `retry_async` (tenacity), `new_nanoid` / `new_uuid7`, `Clock` / `FixedClock`, `AsyncCircuitBreaker` |
| `testing` | `FakeUnitOfWork`, `InMemoryRepository`, JWT test-token factory, `assert_routes_protected` |

## Configuration and environments

`BaseAppSettings` loads `.env` first, then `.env.{environment}`, which
overrides it. The environment itself is resolved in this order:

1. an explicit argument to `resolve_environment()`
2. the real `ENVIRONMENT` process environment variable
3. `ENVIRONMENT` inside `.env`
4. `dev`

Step 3 matters more than it looks. Declaring `ENVIRONMENT=production` in `.env`
without exporting it is the natural thing to do, and reading only `os.environ`
resolves that to `dev` — so `.env.production` never loads, while `.env` still
sets `settings.environment` to `production`. The service then reports itself as
production, passes `is_production` checks, and talks to development
infrastructure, with nothing in the logs to say so.

An invalid value (`prod`, say) raises and names where it came from, rather than
falling back to `dev` and quietly relaxing security. `get_environment()` and
`settings.environment` resolve identically, so they cannot disagree, and
start-up fails outright if the env files were chosen for one environment while
the settings claim another.

```bash
ENVIRONMENT=production   # exported, or in .env — either works
```

[`.env.example`](.env.example) is the full reference: every setting this
library reads, with its real default and a note on the ones whose default is
wrong outside a laptop. Copy it into a service and uncomment what you change:

```bash
cp .env.example .env
```

Every key is live and set to the value pycommon already uses, so a fresh copy
changes nothing — it starts the service exactly as an empty `.env` would, and a
test keeps that true. Eight keys are commented out instead: those are the
settings whose default is *off*, and no value expresses that — an empty number
fails to parse and an empty string is a different thing from unset. Each shows
an example of the shape it wants. Two things the file spells out that cost
people an afternoon otherwise: nested keys follow the **field name your
service declares** (`postgres: DatabaseSettings` is what makes the prefix
`POSTGRES__`, and only `http`, `server` and `cors` exist without you declaring
them),
and list values must be JSON — `CORS__ORIGINS=a,b` raises `SettingsError` at
start-up, `CORS__ORIGINS=["a","b"]` is the form.

Tests walk the settings classes and the file in both directions, so a setting
added without documenting it — or a key left behind after one is removed —
fails CI rather than a consuming service. A further one asserts that every
settings group `pycommon.config` exports is among the classes those checks
walk, since that list is written by hand and would otherwise be the way the
guarantee quietly narrows.

## Middleware configuration

Everything that differs between deployments is a setting, not a function
argument, so an operator changes it without asking for a release. Three nested
groups are on `BaseAppSettings` itself, since every HTTP service needs them:

| Env key | Default | What it does |
|---|---|---|
| `HTTP__TIMEOUT_SECONDS` | unset | per-request ceiling; unset installs no timeout |
| `HTTP__CONTENT_SECURITY_POLICY` | unset | CSP header; unset emits none |
| `HTTP__HSTS` | `true` | emit HSTS on HTTPS requests |
| `HTTP__HSTS_MAX_AGE` | `31536000` | HSTS max-age |
| `HTTP__MAX_BODY_BYTES` | unset | reject request bodies larger than this |
| `HTTP__GZIP_MIN_SIZE` | unset | gzip responses at least this many bytes; unset installs no compression |
| `HTTP__IDEMPOTENCY_TTL_SECONDS` | `86400` | how long a replayable response is kept |
| `HTTP__PROBLEM_TYPE_BASE_URL` | unset | prefix for RFC 9457 `type` URIs; unset emits path-absolute ones |
| `CORS__ORIGINS` | `["http://localhost:5173"]` | allowed browser origins (JSON array) |
| `CORS__ALLOW_CREDENTIALS` | `true` | allow cookies / `Authorization` cross-origin |
| `CORS__ALLOW_METHODS` / `CORS__ALLOW_HEADERS` | `["*"]` / `["*"]` | allowed methods and headers |
| `SERVER__HOST` / `SERVER__PORT` | `0.0.0.0` / `8000` | uvicorn bind |
| `SERVER__FORWARDED_ALLOW_IPS` | unset | peers whose `X-Forwarded-*` to trust |
| `SERVER__DRAIN_DELAY_SECONDS` | `0` | keep serving this long after SIGTERM |

```python
apply_standard_middleware(app, settings)      # reads settings.http and settings.cors
run_from_settings("main:app", settings.server)
```

Every default is off or safe. A timeout nobody chose would turn working slow
endpoints into 504s on upgrade, and a CSP nobody chose would blank out `/docs`.

`metrics` stays an argument to `apply_standard_middleware` rather than becoming
a setting: whether a service records HTTP metrics at all is structural, not
something that differs between staging and production.

## Correlation IDs

Every HTTP request gets an `X-Request-ID` (generated or propagated). It is:

- echoed in the response header
- bound into structlog contextvars (every log line)
- set as span attribute `http.request.id`
- forwarded on outbound HTTP via `create_http_client`
- forwarded on outbound gRPC (all four RPC shapes) by `request_id_client_interceptors` / read on inbound by `RequestIdServerInterceptor`, which binds it for the duration of the handler (streams included) and restores the previous context afterwards

`GrpcChannelPool` and `GrpcServer` both attach OTel interceptors by default, so `traceparent` flows in *and* out — pass `use_otel_interceptor=False` to either if you instrument gRPC yourself.

`trace_id` (W3C `traceparent`, automatic via OTel instrumentation) is the primary distributed correlation ID; `X-Request-ID` is the human-friendly complement for clients and log grep.

## Metrics

`setup_telemetry` installs a `MeterProvider` alongside the tracer and pushes metrics to the same OTLP endpoint. Traces let you debug one request; metrics are what you alert on. Two RED instruments come for free:

| Instrument | Attributes |
|---|---|
| `http.server.request.duration` (histogram, `s`) | `http.request.method`, `http.response.status_code`, `http.route`, `error.type` |
| `http.server.active_requests` (up-down counter) | `http.request.method` |
| `rpc.server.duration` (histogram, `ms`) | `rpc.system`, `rpc.service`, `rpc.method`, `rpc.grpc.status_code`, `error.type` |

HTTP metrics come from `MetricsMiddleware` (on by default in `apply_standard_middleware`, disable with `metrics=False`); gRPC metrics from `MetricsServerInterceptor` (`GrpcServer(use_metrics_interceptor=False)` to disable). Both label by the **route template** and a bounded method set — never the raw path or verb, which are caller-controlled and would let anyone mint unbounded time series in your metrics backend. Requests that match no route carry no `http.route` at all rather than their path. Probe traffic (`/health`, `/live`, `/ready`) and `/metrics` are excluded so they do not dominate the request rate.

The instruments are created from the OTel **API**, so they are no-ops with no measurable cost until a provider exists — a service that exports nothing pays nothing for leaving them on.

To be scraped instead of pushed:

```python
from pycommon.telemetry import build_metrics_router, setup_telemetry

setup_telemetry(app, service_name=..., prometheus_metrics=True)
app.include_router(build_metrics_router())   # GET /metrics
```

Workers, gRPC servers and CLIs have no FastAPI app; they call `setup_metrics(service_name=...)` directly. Call `shutdown_telemetry()` on shutdown — it flushes both providers, and the unflushed window is exactly the one where a crashing pod's metrics matter most.

## Deploying behind a proxy

**If you run behind an ingress or load balancer, you must tell the ASGI server which peers to trust** — otherwise three things fail silently.

uvicorn only honours `X-Forwarded-For` / `X-Forwarded-Proto` when the immediate peer is listed in `forwarded_allow_ips`, which defaults to `127.0.0.1`. In Kubernetes the peer is the ingress pod, never loopback, so the headers are ignored and:

- `scope["scheme"]` stays `http`, so **`SecurityHeadersMiddleware` never emits HSTS** even though `hsts=True` is the default
- `scope["client"]` stays the ingress address, so **every anonymous caller shares one rate-limit bucket** — `build_rate_limit_dep(..., times=10, seconds=60)` on `/login` becomes a global 10/min for the whole internet, which one bot can exhaust for everyone
- access logs record the ingress address instead of the caller

Fix it once, at the server:

```bash
FORWARDED_ALLOW_IPS='10.0.0.0/8'   # or the ingress CIDR / '*' if the proxy is the only reachable peer
```

or explicitly:

```python
run_from_settings("main:app", settings.server)   # SERVER__FORWARDED_ALLOW_IPS
```

Prefer the narrowest value that matches your proxy. `'*'` trusts `X-Forwarded-For` from *any* peer, which is safe only when nothing but the proxy can reach the port.

pycommon reads the resolved value through a single helper, `pycommon.http.middleware.client_ip`, shared by the access log and the rate-limit dependency so both always agree on who the caller is. It deliberately never parses `X-Forwarded-For` itself: any client can send that header, and trusting it unconditionally lets callers forge their own address in your logs and rate-limit buckets.

## Content Security Policy

`SecurityHeadersMiddleware` emits the OWASP baseline headers by default;
`Content-Security-Policy` is opt-in because there is no value that is right for
every service. The policy a JSON API wants blanks out Swagger UI and ReDoc,
which load their assets from a CDN, and a policy permissive enough for them
protects nothing:

```bash
HTTP__CONTENT_SECURITY_POLICY="default-src 'none'; frame-ancestors 'none'"
```

`pycommon.http.middleware.API_CONTENT_SECURITY_POLICY` holds that value if you
would rather set it from code.

`API_CONTENT_SECURITY_POLICY` is `default-src 'none'; frame-ancestors 'none'`.
If you serve interactive docs in production, exclude their path or widen the
policy to permit their CDN.

## Response compression

Off unless asked for. `HTTP__GZIP_MIN_SIZE=500` installs Starlette's
`GZipMiddleware`; unset, nothing is installed and responses go out exactly as
they did before. Compression changes the bytes on the wire for every endpoint at
once, which is not something a library should switch on for its consumers during
an upgrade.

It is Starlette's implementation rather than one of ours — it already excludes
`text/event-stream` and pre-compressed media types (images, video, zip), sets
`Vary: Accept-Encoding`, and moves payloads over 128 KiB onto a worker thread so
a large response cannot stall the event loop.

What this library decides is *where it sits*: inside the metrics and request
context layers, outside the timeout and idempotency ones.

- **Outside idempotency** is the one that would bite. `IdempotencyMiddleware`
  stores a response and replays it for a repeated key; a compressed body in that
  store would be replayed byte-for-byte to a client that never sent
  `Accept-Encoding: gzip` and cannot decode it. The store holds the plain body
  and each request is compressed on its own terms.
- **Inside metrics and the access log**, so the time compression costs appears
  in `http.server.request.duration` and `duration_ms` rather than hiding outside
  the numbers you alert on.
- **Outside the timeout**, because the deadline is a ceiling on the handler, not
  on serialising what it returned.

Two things worth checking before switching it on:

- **Your ingress may already do it.** nginx, Envoy and ALB all compress on the
  way out. Enabling it here as well spends CPU on work that gets thrown away.
- **BREACH.** Compressing a response that mixes a secret (a CSRF token, an API
  key) with attacker-influenced reflected input lets the compressed size leak
  the secret. That is a reason to leave compression off for those specific
  endpoints, not a reason to avoid it everywhere.

## Caching

Cache-aside for **values**, not HTTP responses — it takes no `Request`, so the same code works in a route, a gRPC servicer, a Celery worker or a CLI job.

```python
from pycommon.cache import Cache, cached, pydantic_serializer

@cached(redis, namespace="products", ttl_seconds=300)
async def get_product(product_id: str) -> dict:
    return await repository.get(product_id)

await get_product.invalidate("abc-123")     # after a write

# or explicitly
cache = Cache(redis, namespace="products", ttl_seconds=300)
product = await cache.get_or_set(product_id, lambda: repository.get(product_id))
await cache.delete(product_id)
await cache.clear()                          # whole namespace
```

- **Stampede protection is on by default.** When a popular key expires under load, only one caller computes it; the rest wait briefly and read what it stored. Without it every concurrent request goes to the database at once.
- **`ttl_seconds` is required**, not defaulted. An entry with no expiry is a leak plus permanently stale data if an invalidation is ever missed — pass `None` explicitly for entries you always invalidate by hand.
- **Fails open.** If Redis is unreachable the factory runs and its value is returned uncached. So does a poisoned entry: it counts as a miss instead of failing every request until the TTL expires.
- **Serialization** defaults to JSON (dict / list / scalars). For models use `serializer=pydantic_serializer(Product)`, which returns the model, not a dict.
- Keys are `cache:{namespace}:key` — the braces are a Redis Cluster hash tag, keeping a namespace on one slot so `clear()` scans a single node.

## Rate limiting

```python
from pycommon.cache import RedisRateLimiter, RedisSlidingWindowRateLimiter
from pycommon.http.middleware.rate_limit import build_rate_limit_dep

rate_limited = build_rate_limit_dep(RedisRateLimiter(redis), "10/second")

@router.post("/login", dependencies=[Depends(rate_limited)])
async def login(): ...
```

Rates accept `"100/minute"`, `"10/15seconds"`, `"100 per 2 minutes"`, `"5/s"`, or explicit `times=`/`seconds=`. Every response carries `X-RateLimit-Limit` / `-Remaining` / `-Reset`; 429s add `Retry-After`.

| Limiter | Trade-off |
|---|---|
| `RedisRateLimiter` | Fixed window — one counter per key, cheapest. Allows up to `2 × times` across a window boundary. |
| `RedisSlidingWindowRateLimiter` | Sliding window log — no boundary burst, at one sorted-set entry per allowed request. Scores come from Redis `TIME`, so skewed instance clocks cannot corrupt a shared window. |
| `InMemoryRateLimiter` | Per-process, for dev and tests. Bounded LRU (`max_keys`). |

Both Redis limiters **fail open**: if Redis is unreachable the request is allowed, a warning is logged, and `RateLimitResult.degraded` is set so degraded traffic stays visible in metrics rather than looking like traffic that genuinely passed. Pass `fail_open=False` where exceeding the limit is worse than rejecting traffic (payment retries, SMS sending).

Rate limits are only per-caller if the client IP is resolved correctly — see [Deploying behind a proxy](#deploying-behind-a-proxy).

## Libraries we deliberately don't vendor

Both belong at the **service layer**, not here. Adding either to pycommon would push its opinions onto every service at once.

**fastapi-guard** — a full security suite that would conflict with our middleware stack (CORS, headers, auth). Services needing IP ban / geo-block / bot detection can add it themselves, or better: enforce those at the API gateway. Business rate limiting lives in `pycommon.cache` + `build_rate_limit_dep`.

**fastapi-redis-sdk** (official Redis SDK) — offers HTTP response caching with ETag/304, which pycommon does not. Worth adding to a service that needs it, but not to pycommon, because:

- it is FastAPI-coupled (`FastAPIRedis(app).lifespan()`, everything via `Depends()`, cache keys derived from `Request`), while `pycommon.cache` must also work from gRPC servicers, Celery workers and CLI jobs
- its `.lifespan()` overlaps `build_lifespan`, and its flat `REDIS_*` env keys conflict with `BaseAppSettings`' nested `REDIS__URL`
- its 429 is not problem+json, which would reopen the error-contract inconsistency this library just fixed
- it has no distributed lock, so it does not replace `redis_lock` either
- it requires Redis 7.4+ and is pre-1.0

Ideas worth borrowing from it are already implemented here: fail-open limiting with a `degraded` flag, the rate DSL, and `X-RateLimit-*` headers.

## Quick usage

```python
from pycommon.config import BaseAppSettings, DatabaseSettings, ProfilerSettings
from pycommon.http import (
    build_health_router,
    build_problem_types_router,
    register_exception_handlers,
)
from pycommon.http.middleware import apply_standard_middleware
from pycommon.logging import setup_logging
from pycommon.persistence import (
    create_engine_and_sessionmaker,
    database_lifespan_resource,
    migration_lifespan_resource,
)
from pycommon.runtime import build_lifespan, create_base_app, run_uvicorn
from pycommon.telemetry import enable_profiler

class Settings(BaseAppSettings):
    app_name: str = "my-service"
    postgres: DatabaseSettings = DatabaseSettings()
    profiler: ProfilerSettings = ProfilerSettings()

settings = Settings()
setup_logging(
    level=settings.log_level,
    service_name=settings.app_name,
    environment=settings.environment.value,
)

engine, session_factory = create_engine_and_sessionmaker(settings.postgres)

app = create_base_app(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=build_lifespan(
        [
            migration_lifespan_resource(settings.postgres),  # no-op unless auto_migrate=True
            database_lifespan_resource(engine),
        ]
    ),
    is_dev=settings.is_dev,
)
register_exception_handlers(app, problem_type_base_url=settings.http.problem_type_base_url)
apply_standard_middleware(app, settings)
enable_profiler(app, settings.profiler, environment=settings.environment.value)
app.include_router(build_health_router([]))
app.include_router(build_problem_types_router(problem_type_base_url=settings.http.problem_type_base_url))

if __name__ == "__main__":
    run_uvicorn("main:app", reload=True)
```

Raise application errors with shared `ErrorCode` values (HTTP status is fixed per code):

```python
from pycommon.errors import AppError

raise AppError.input("Order 42 does not exist")
# → application/problem+json with type=/problems/input, error_code=3, status=400
```

## Route protection

Two layers, attached at two different places, because they fail in two different
ways.

**Authentication goes on the router.** Every endpoint under `/api/v1` wants a
valid JWT; there is no interesting per-route decision, and the only realistic
mistake is forgetting one — which publishes an endpoint with nothing failing to
say so. Putting it on the router means routes inherit it structurally, nested
routers included.

**Authorization goes on the route.** There is no correct default for "who may
delete this", so it is written where it applies and read in review. Folding it
into a router would breed a router per combination of rights and turn a decision
into something inherited without being reread.

```python
from fastapi import APIRouter, Depends
from pycommon.security import (
    Auth, HasRole, HasScope, KeycloakTokenValidator,
    TokenClaims, internal_router, protected_router,
)

auth = Auth(KeycloakTokenValidator(settings.keycloak))

api      = protected_router(auth, prefix="/api/v1", tags=["api"])
internal = internal_router(settings, prefix="/internal", tags=["internal"])
public   = APIRouter(prefix="/api/public/v1", tags=["public"])

@api.get("/me")                                    # authentication only
async def me(user: TokenClaims = Depends(auth.current_user)):
    return {"sub": user.sub}

@api.delete("/users/{uid}", dependencies=[Depends(auth.require_roles("admin"))])
async def delete_user(uid: str): ...

@api.post("/orders", dependencies=[Depends(auth.requires(
    HasRole("admin") | HasScope("orders:write")))])
async def create_order(): ...
```

A public router is a plain `APIRouter` — there is nothing for this library to
add, and a wrapper would only be a name.

### Requirements

| | |
|---|---|
| `HasRole("a", "b")` | holds any one of these roles (realm or client) |
| `HasScope("x", "y")` | token carries any one of these OAuth2 scopes |
| `Custom(fn, "why")` | any predicate over the claims, including `claims.raw` |
| `a \| b` / `a & b` | combine them; nest freely |

Each primitive takes several values meaning *any of these*; the operators join
different kinds. That composition is the point — `require_roles` and a
hypothetical `require_scopes` could never between them express `role OR scope`,
and one more function per axis never fixes that.

A failed check answers 403 naming the rule
(`Insufficient permissions; requires: role:admin OR scope:orders:write`). That
does tell an authenticated caller what the policy is: a deliberate trade, on the
grounds that a 403 nobody can debug costs more here than the rule being known to
someone who already holds a token.

There is no negation. "Anyone except …" is a denial list, and denial lists fail
open the moment someone adds a role nobody thought about.

**Role and scope are not interchangeable.** A role says *who* the caller is — it
belongs to the user, or to a service account, which Keycloak gives client roles
just like a person. A scope says *what the client application* may do on their
behalf. A delegated token is correctly checked against both. Fine-grained rights
are usually modelled as client roles in Keycloak, so `HasRole("orders:write")`
is ordinary rather than a misuse — and there is deliberately no `HasPermission`,
because Keycloak emits no permission claim of its own and a deployment that maps
one has `Custom` to read it.

### Internal routes

`internal_router` installs an `X-API-Key` check when `HTTP__INTERNAL_API_KEY` is
set, and installs nothing when it is not — reasonable when the network already
keeps those routes unreachable, but a decision rather than an accident, so it is
logged at construction and reported by the audit below.

### Proving it

Forgetting is the failure this design is shaped around, so check it in the
service's own suite:

```python
from pycommon.testing.routes import PYCOMMON_PUBLIC_PREFIXES, assert_routes_protected

def test_no_route_is_accidentally_public():
    assert_routes_protected(
        build_app(),
        public_prefixes=("/api/public/v1", *PYCOMMON_PUBLIC_PREFIXES),
    )
```

It reads the OpenAPI document and fails naming every operation that declares no
security scheme. Two things it does not do: it says nothing about
*authorization* (a route that authenticates but forgets its `requires` passes —
there is no default rule to compare against), and it only sees protection that
declares a scheme, so a route guarded by a plain dependency has to be listed.

### Why this is not middleware

Middleware is the obvious first idea and it is wrong four times over. It runs
*before* routing, so exemptions can only be path patterns — the classic bypass
surface. An `HTTPException` raised in middleware never reaches FastAPI's
handlers, which live inside the router, so its 401 would not be `problem+json`,
would carry no request ID, and would miss the access log; raised from a
dependency it does all three. Middleware is invisible to OpenAPI, so Swagger's
Authorize button and the documented 401 both disappear. And it cannot inject
`TokenClaims` into a handler signature — claims would travel through
`request.state`, untyped.

## Error contract

`register_exception_handlers` makes **every** error response RFC 9457 Problem Details — not just `AppError`:

| Raised | Status | Result |
|--------|--------|--------|
| `AppError.*` | per `ErrorCode` | `application/problem+json` |
| `RequestValidationError` (FastAPI) | 422 | problem+json; field errors in the `errors` member |
| `HTTPException` | as raised | problem+json; `exc.headers` preserved (`WWW-Authenticate`, `Retry-After`) |
| anything else | 500 | problem+json; exception details never leak to the client |

`ErrorCode` values: `OK=0`, `SERVER=1`, `DATABASE=2`, `INPUT=3`, `AUTH=4`, `APP_CHECK=5`, `FORBIDDEN=6`, `NOT_FOUND=7`, `CONFLICT=8`, `RATE_LIMIT=9`, `TIMEOUT=10`, `PAYLOAD_TOO_LARGE=11`, `IDEMPOTENCY=12`. A status with no application meaning (405, 418, …) still returns problem+json but omits `error_code` rather than claiming a misleading one.

**Error responses carry the same headers as successful ones** — `X-Request-ID`, CORS, and security headers. This requires `apply_standard_middleware` (or `RequestContextMiddleware` installed inside your CORS/security layers): Starlette runs the `Exception` handler in `ServerErrorMiddleware`, outside every user middleware, so a 500 built there would otherwise reach a cross-origin SPA with no CORS header at all — unreadable, and without the request ID needed to trace it.

Because `RequestContextMiddleware` fully handles unhandled exceptions, they no longer propagate. In tests use `TestClient(app, raise_server_exceptions=False)` and assert on the 500, or pass `RequestContextMiddleware(handle_exceptions=False)` to let them bubble up.

Success envelope (optional):

```python
from pycommon.http import ApiResponse

return ApiResponse.ok({"id": order.id})
```

Set `HTTP__PROBLEM_TYPE_BASE_URL=https://docs.example.com/problems` to emit absolute `type` URIs.

## Persistence notes

**Connection pooling** — `POSTGRES__POOL_RECYCLE_SECONDS` defaults to 1800: pooled connections are dropped and reopened once they reach that age. Whatever sits between the app and Postgres — pgbouncer, a cloud load balancer, a NAT gateway — closes idle connections on its own schedule without telling the pool, and the next checkout then fails with `server closed the connection unexpectedly` at random. **Set this below the shortest idle timeout in front of your database**; the default is wrong if yours is five minutes. `pool_pre_ping` (on by default) catches the same case but pays a round-trip on every checkout, so treat it as the safety net rather than the fix. `POSTGRES__POOL_TIMEOUT_SECONDS` caps how long a request waits for a free connection instead of blocking forever behind an exhausted pool.

**`delete()` bypasses ORM cascades.** `SqlAlchemyRepository.delete` issues a bulk `DELETE` — one round-trip instead of load-then-delete, but `cascade="all, delete-orphan"` relationships are not walked and `before_delete` / `after_delete` listeners never fire. Express cascades as database-level `ON DELETE CASCADE`, or override `delete()` in your subclass.

**Ordering in tests** — `InMemoryRepository.get_list(order_by=...)` takes attribute names (`"created_at"`, `"-created_at"`, or a list of them), since a fake has no SQL to sort with. Hand it a SQLAlchemy column expression and it raises rather than returning an unsorted page that would make the assertion meaningless.

**Query logging** — set `POSTGRES__LOG_QUERIES=true` for structured SQL logs (statement + `duration_ms`) via structlog. Use `POSTGRES__SLOW_QUERY_THRESHOLD_MS=200` to only warn on slow queries. Failed queries are always logged as `db_query_failed` regardless of the threshold — a deadlock or statement timeout is worth seeing however fast it failed. Keep `POSTGRES__LOG_QUERY_PARAMS=false` unless debugging (params may contain PII). `POSTGRES__ECHO=true` remains available for raw SQLAlchemy echo in local dev.

**Migrations** — pycommon provides thin Alembic helpers; each service owns `alembic.ini`, `alembic/env.py`, and `alembic/versions/`.

```bash
uv add "pycommon[persistence,migrations]"
```

```python
from pycommon.persistence import Base, build_alembic_config, upgrade_to_head

# models inherit Base (shared naming convention for autogenerate)
class Order(Base):
    __tablename__ = "orders"
    ...

# CLI / deploy job
upgrade_to_head(settings.postgres, script_location="alembic")
```

In `alembic/env.py`, set `target_metadata = Base.metadata` and prefer `build_alembic_config(settings)` for the URL. Keep `POSTGRES__AUTO_MIGRATE=false` in production and run upgrades from a deploy job; enable it only for local/dev if desired via `migration_lifespan_resource`.

## Idempotency keys

A client whose connection drops after the server committed, but before the
response arrived, cannot tell "the order was created" from "the order was not
created". Its only safe options are to give up or to retry — and retrying
without this creates a second order.

```python
apply_standard_middleware(app, settings, redis=redis)
```

Clients then send `Idempotency-Key: <uuid>` on `POST`/`PATCH`/`DELETE`. A repeat
with the same key replays the first response with `Idempotent-Replay: true`,
without running the handler again.

**A key is scoped to `caller + method + path + key`.** The caller is part of it
because keys are chosen by clients, and two clients will eventually pick the
same one — without scoping, the second would be handed the first's response,
which is a data leak rather than a collision.

**The body is fingerprinted.** Reusing a key with different content returns 409
rather than the first response, because silently discarding the second request
is worse than refusing it.

**5xx responses are not stored.** An idempotency record for a server error would
make that failure permanent for the key: every retry would replay the 500 instead
of getting the second chance the client is asking for.

**It fails closed** when Redis is unreachable — unlike the rate limiter and cache,
which fail open. Those degrade a convenience; this one degrades the guarantee it
exists to provide, and the damage is a duplicate payment rather than a slow page.
Serving 503 is also safe here in a way it is not elsewhere: the client is holding
a key, so it can retry the moment Redis returns. `fail_open=True` where a
duplicate is cheaper than a rejection.

The header is not mandatory — requiring it would break every existing client the
day it is switched on. A service that wants it enforced can check for it in a
dependency.

## Request body limits

A JSON endpoint with no limit buffers whatever it is sent. `json.loads` on a
gigabyte allocates several more, so **one request from one client** can take the
process down — no volume required, which is what separates this from rate
limiting.

```bash
HTTP__MAX_BODY_BYTES=1048576   # 1 MiB
```

Oversized requests get Problem Details:

```json
{"type": "/problems/payload-too-large", "title": "Content Too Large",
 "status": 413, "error_code": 11, "request_id": "…"}
```

Two checks, because either alone is insufficient:

- **`Content-Length`**, when present, is rejected before a byte of body is read.
  The cheap path, and it covers ordinary clients.
- **The streamed bytes are counted anyway.** `Content-Length` is optional under
  chunked transfer encoding and is in any case a claim by the caller — a limit
  that trusts it stops honest clients and nobody else.

For endpoints that legitimately take large bodies — file upload, bulk import —
exempt those paths rather than raising the global limit, which would hand every
other endpoint the same allowance:

```python
app.add_middleware(BodySizeLimitMiddleware, max_bytes=..., exclude_paths=["/upload"])
```

Off by default: a service already accepting large uploads would start rejecting
them on upgrade.

## Request timeouts

A handler blocked on an upstream that never answers holds its connection, its
database session and its worker slot indefinitely. Enough of them and the
service stops serving anything while every health check still passes — the
process is fine, it is just entirely occupied.

```bash
HTTP__TIMEOUT_SECONDS=15
```

On expiry the handler is **cancelled**, not merely abandoned, and the client
gets Problem Details:

```json
{"type": "/problems/timeout", "title": "Gateway Timeout", "status": 504,
 "error_code": 10, "request_id": "…"}
```

Answering 504 while the work continued would leave the session checked out and
the upstream call in flight — the same leak, minus the visibility.

**The clock covers time-to-first-byte, not the whole response.** Once the
handler produces a status line the deadline is lifted and the body streams for
as long as it needs, so server-sent events, large downloads and streamed
exports are unaffected. A wall-clock limit on the complete response would kill
exactly the endpoints that legitimately run long.

Set it **below** whatever your ingress uses. Then the timeout that fires is the
one that can explain itself: this one returns Problem Details with a request ID
that appears in your logs, while a proxy timeout returns the proxy's error page
and leaves the handler running.

Probe and metrics paths are excluded by default (`exclude_paths`) — probes carry
their own timeouts, and a readiness check racing a middleware timeout produces
two answers to one question.

Off by default: the right ceiling depends on what the service does, and one set
too low turns working slow endpoints into errors.

## Graceful shutdown and draining

Kubernetes removes a pod from its Service endpoints and sends SIGTERM **at the
same time**, and that removal then has to propagate to kube-proxy and to every
ingress controller. A process that starts shutting down the moment it is
signalled stops accepting connections while traffic is still being routed to
it. Those requests fail — the connection-refused blip on every deploy.

```bash
SERVER__DRAIN_DELAY_SECONDS=10
```

On the first SIGTERM the process is marked draining and **keeps serving
normally** for that long. Only then does uvicorn's graceful shutdown begin:
stop accepting, finish in-flight requests, run lifespan shutdown. A second
signal skips the wait.

While draining:

| Endpoint | Answer | Why |
|---|---|---|
| `/health/ready` | **503** `{"status": "draining"}` | tells the load balancer to stop sending traffic |
| `/health/live` | **200** | a draining process is not broken |

`/live` staying 200 is not an oversight. If liveness failed during a drain, the
kubelet would restart the container mid-shutdown and kill exactly the in-flight
requests the drain exists to protect. Readiness answers "send me traffic";
liveness answers "am I broken". Only the first changes here.

`/health/ready` also skips its dependency checks while draining — their result
cannot change the answer, and a dependency that has already begun shutting down
would make the probe slow at the moment the balancer is trying to learn it
should stop using this instance.

Two constraints:

- `drain_delay_seconds` **must be shorter than `terminationGracePeriodSeconds`**
  (default 30), or the kubelet sends SIGKILL mid-drain and you have made things
  worse. 5–15 seconds suits most setups; the right value is how long your
  ingress takes to notice.
- It cannot be combined with `reload=True`, which raises. Reload runs a
  supervisor that respawns the worker, so the worker's signal handling never
  sees SIGTERM — draining would look configured and do nothing.

A Kubernetes `preStop` hook (`sleep 10`) achieves the same delay with no
application code, and is worth preferring if you can set one. This exists for
services that cannot, and it additionally makes readiness tell the truth.

Any code can ask, including gRPC servicers and workers:

```python
from pycommon import is_draining, begin_draining
```

## Pagination

`Page` / `PageMeta` and the cursor codec live in `pycommon.http.pagination`; the
two helpers that turn a `Select` into a `Page` live in
`pycommon.persistence.pagination`, because they take a session rather than a
request and are just as useful from a worker or a CLI job.

```python
from pycommon.persistence import paginate_cursor, paginate_offset

page = await paginate_offset(session, select(User), limit=20, offset=40)
page = await paginate_cursor(session, select(User), key_column=User.id, limit=20, cursor=cursor)
```

Which to use:

| | `paginate_offset` | `paginate_cursor` |
|---|---|---|
| Jump to page N | yes | no |
| Total count | optional (`with_total`) | no |
| Stable under concurrent inserts | **no** | yes |
| Cost at high offsets | grows | flat |

Offset pagination shifts under the reader: a row inserted before the current
offset moves everything down, so items get skipped or repeated between pages.
For a feed that changes while it is being read, use the cursor.

`key_column` must be **unique and sortable** — a UUIDv7 or bigint primary key.
A non-unique column such as `created_at` silently drops or repeats rows sharing
a value at a page boundary. `limit` is clamped to `max_limit` (default 100),
since it usually comes straight from a query string and an unbounded one asks
the database for the whole table.

`with_total=False` skips the count query. The count scans the whole filtered
set, which is free on a small table and the most expensive thing on the page
once it is not.

## ORM mixins

```python
from pycommon.persistence import Base, SoftDeleteMixin, TimestampMixin, UUIDv7PrimaryKeyMixin

class User(Base, UUIDv7PrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "users"
```

- **`UUIDv7PrimaryKeyMixin`** — time-ordered `id`, so inserts land at the end of
  the index rather than scattering across it the way v4 does. Generated by the
  application at INSERT, so no `RETURNING` round trip; the attribute is `None`
  until the session flushes.
- **`TimestampMixin`** — `created_at` / `updated_at` as `TIMESTAMP WITH TIME
  ZONE`, defaulted by the *database* clock. Application instances with drifting
  clocks would otherwise write timestamps that do not order consistently, and
  ordering is what these columns are for.
- **`SoftDeleteMixin`** — `deleted_at` plus `is_active()` / `is_deleted()`
  predicates. Deliberately **not** a global query filter: one that applies
  itself to every query is one people forget exists, and the symptom is a report
  that comes out short with nothing at the call site to explain it.

```python
stmt = select(User).where(User.is_active())
```

## Governance

This library is shared by multiple services, so a change here ships to all of them at once:

- **Backward compatibility first.** Breaking a public API requires a version bump and a migration note. Prefer additive changes (new parameters with defaults, new modules).
- **Semantic versioning.** Consumers pin a tag (`@v0.1.0`); never re-tag. While the major version is `0`, SemVer permits a *minor* bump to break compatibility — the migration note is what makes that safe, not the version number. See [RELEASING.md](RELEASING.md).
- **No domain logic.** Business entities, service-specific constants, or third-party partner integrations belong in the owning service, not here.
- **No silent failures.** Infrastructure setup errors must be logged or raised, never swallowed.

## Development

```bash
make install      # uv sync --extra all --extra dev
make check        # lint + format check + mypy --strict + tests (what CI runs)
```

`make help` lists every target. See [CONTRIBUTING.md](CONTRIBUTING.md) for
branching, commit and pull-request conventions, and [RELEASING.md](RELEASING.md)
for cutting a release.

### Integration tests

`tests/integration` runs against real Redis, Postgres, Jaeger, MinIO and
Keycloak. Each group skips unless its environment variable is set, so a plain
`make test` stays offline.

```bash
make infra-up                 # starts all five, waits until healthy
make test-integration-local   # runs the suite against them
make infra-down               # stops them and deletes the data
```

Point them only at throwaway instances: the Redis fixture calls `FLUSHDB`, the
Postgres one drops and recreates its tables, and the Keycloak realm is
re-imported from scratch.

### Working on the Keycloak fixture

The realm lives in `tests/integration/keycloak-realm.json` and is imported when
the container starts, so there is no setup step — `make infra-up` is all of it.
Admin console at <http://localhost:8080>, `admin` / `admin`; realm
`pycommon-test`; test user `alice` / `alice-password`.

It holds two clients that differ by exactly one thing — a dedicated audience
mapper — because that difference is what the audience tests are built on. It is
hand-written and minimal rather than a Keycloak export: an export of this same
realm is over 2700 lines of defaults, and the file's job is to let a reviewer
*see* that one difference.

After editing it:

```bash
make infra-down && make infra-up
```

`--import-realm` **skips a realm that already exists**, so `docker compose
restart` would quietly keep testing the old one. `make infra-down` uses
`down -v`, which is what clears it.

`make keycloak-export` dumps the *running* realm to `/tmp` — for answering
"what did Keycloak make of what I wrote" when a claim does not come out as
expected. It never touches the fixture.

### Scoping a service token

`HasScope` reads the standard OAuth2 `scope` claim, and out of the box a
Keycloak token carries only its client's default scopes — `profile email`. So a
rule like `HasScope("orders:write")` denies everyone until two things are true:

**1. The scope exists in the realm and is optional on the client.** Create a
client scope named `orders:write`, then assign it to the client under *Optional*
— not *Default*. Optional means it is granted only when asked for, which is what
makes the scope narrow anything at all. Keycloak answers `invalid_scope` for a
scope no client scope defines, so this step cannot be skipped.

**2. The caller asks for it.** For service-to-service calls that is
`KEYCLOAK__TOKEN_SCOPE`, which `ClientCredentialsTokenProvider` sends with the
grant:

```bash
KEYCLOAK__TOKEN_SCOPE="orders:write orders:read"
```

Unset, no `scope` parameter is sent and the request is byte-identical to what it
was before the setting existed. Asking for one scope does not narrow away the
defaults — `profile` and `email` still arrive.

> **If you script the realm:** declaring `clientScopes` in a realm-import JSON
> **replaces Keycloak's entire built-in set** rather than adding to it. Doing
> that drops `profile`, `email` and `roles`, and tokens then come back with no
> `realm_access` at all — valid, and authorising nothing. Add client scopes
> through the admin console or the Admin API instead, or carry every built-in
> scope in the file.

For user-facing flows the front end requests the scope in its authorization
request; pycommon is not involved.

### What `verify_aud` actually checks

Worth knowing before you rely on it, and pinned by
`tests/integration/test_keycloak_integration.py`:

Keycloak fills `aud` from the clients the **subject holds roles on**, not from
the client that requested the token — that one is `azp`, which pycommon does not
check. So `KEYCLOAK__VERIFY_AUD=true` does **not** establish that a token was
issued to your client: any other client in the realm can obtain one your API
will accept, for any user holding a role on your API.

What it does reject is a subject with no role on your client at all. Keycloak
emits `aud: "account"` for them, and the token fails as "Invalid or expired
token" with nothing in the message pointing at the audience — the confusing
day-one 401 against an untuned realm. A dedicated audience mapper on your client
puts your `clientId` in `aud` regardless of roles, which is worth adding.

**To pin which client obtained the token, use `KEYCLOAK__ALLOWED_AZP`.** `azp`
is the claim that names the requesting client — the one people expect `aud` to
be:

```bash
KEYCLOAK__ALLOWED_AZP='["web-spa","mobile-app"]'
```

Empty, the default, accepts any, so nothing changes until you set it. Once set, a
token from a client not on the list is rejected with *"Token was not issued to a
client this service accepts"*, and so is a token carrying no `azp` at all:
having declared which clients you trust, one that will not say where it came
from is not among them.

It is a setting on the validator rather than a `Requirement` because it is
uniform for the whole service — which clients may reach it at all is a trust
boundary, not a per-endpoint decision — so it applies to every token, including
the ones `internal_router` and `optional_user` see.

Weigh it before switching it on. With a single client in the realm it buys
nothing; `azp` is a claim OIDC defines for ID tokens that Keycloak also puts on
access tokens; and every legitimate front end has to be listed and kept listed.

## License

[MIT](LICENSE) © 2026 Hieu Pham Trung.

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). By opening a
pull request you agree that your contribution is licensed under the same terms.
