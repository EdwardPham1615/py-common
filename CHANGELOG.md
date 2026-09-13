# Changelog

All notable changes to pycommon are recorded here. This library is shared by
multiple services, so every breaking change carries a migration note.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- **A gRPC request ID leaked into every later call on the same context.**
  `RequestIdServerInterceptor` bound `request_id` to structlog contextvars in
  `intercept_service` and never unbound it. Two problems, one line: that hook
  only *resolves* the handler and returns before it runs, and nothing ever
  unwound the binding — so the ID outlived its RPC and whatever inherited that
  context next (a later RPC, a background task, an outbound call reading
  `current_request_id()`) logged and propagated a stale one. A wrong
  correlation ID is worse than none: it silently merges two unrelated requests
  in log search, with nothing to indicate it happened.

  The binding now wraps the handler itself — all four RPC shapes, held across
  every message a streaming handler yields, unwound when it ends or raises, and
  restoring (not clearing) any context the caller had bound around the call.

- **Database passwords containing a space were silently wrong.** `DatabaseSettings`
  built its DSN with `quote_plus`, which is the encoding for query strings, where
  a space becomes `+`. In the userinfo part of a URL a `+` is a literal plus and
  no parser turns it back into a space, so a password like `p@ss word` was sent
  as `p@ss+word` and the service could not connect at all — to Postgres or, via
  Alembic, to migrations. Now `quote(..., safe="")`, with a test that asserts a
  URL parser recovers the original credentials rather than asserting on the
  encoded substring, which is what let this through.

- **BREAKING-ish — Alembic no longer crashes on a password that needs escaping.**
  `build_alembic_config` handed the DSN straight to Alembic, which keeps its
  options in a `ConfigParser` where `%` starts an interpolation.
  `DatabaseSettings` percent-encodes credentials, so any password containing
  `@`, `:`, `/`, `#` or a space arrived as `%40`, `%3A`, `%2F`, `%23`, `%20` —
  and configparser rejected the value outright with
  `ValueError: invalid interpolation syntax`. Every migration entry point
  (`upgrade_to_head`, `downgrade`, `migration_lifespan_resource`) raised before
  touching the database, for a large share of realistic generated passwords.
  Percent signs are now doubled on the way in, which configparser restores on
  read.

- **`pycommon[persistence]` now installs the driver its DSN names.**
  `DatabaseSettings.async_dsn` hardcodes `postgresql+asyncpg` and `sync_dsn`
  hardcodes `postgresql+psycopg`, but neither driver was declared, so a service
  that installed exactly what the README told it to got
  `ModuleNotFoundError: No module named 'asyncpg'` at the first call to
  `create_engine_and_sessionmaker()`. `asyncpg` joins the `persistence` extra and
  `psycopg[binary]` joins `migrations`. Found by writing the first test that
  builds a real engine.

- **`ENVIRONMENT` declared in `.env` now selects the right env file.**
  `resolve_env_files()` read `ENVIRONMENT` from `os.environ` only, so putting
  `ENVIRONMENT=production` in `.env` — without also exporting it — resolved to
  `dev`, and `.env.production` was never loaded. `.env` still set
  `settings.environment` to `production`, so the service reported itself as
  production, passed `is_production` checks, and ran against development
  database and broker addresses. Nothing logged a warning, because from each
  component's point of view nothing had gone wrong.

  Resolution order is now: explicit argument, then the real `ENVIRONMENT`
  variable, then `ENVIRONMENT` inside `.env`, then `dev`. An invalid value
  raises and names where it came from.

  *Migration:* if a deployment relied on the old behaviour — `ENVIRONMENT` in
  `.env` while intending the `dev` files to load — it now loads
  `.env.{environment}` instead. Check which env files each environment
  actually ships before upgrading. `BaseAppSettings` also raises on start-up if
  the env files were resolved for one environment while the loaded settings
  claim another, which only happens when an environment-specific file
  reassigns `ENVIRONMENT`.

- **`get_environment()` and `settings.environment` can no longer disagree.**
  Both now resolve through `resolve_environment()`; previously the first read
  `os.environ` and the second read the env files, so they answered differently
  in exactly the case above.

- **One definition of the current request ID.** `problem.py` read
  `request.state.request_id` while the response envelope, the httpx client and
  the gRPC interceptors read structlog contextvars. Problem Details now falls
  back to the logging context when the scope carries nothing, so an error body
  no longer reports `request_id: null` for a request whose logs have one.

- **Readiness checks run concurrently.** `/health/ready` awaited each check in
  turn, so its worst case was the *sum* of the timeouts — three dependencies at
  the 5s default is 15 seconds, well past the 1 to 5 seconds a Kubernetes
  readiness probe waits. The probe timed out and marked the pod unready before
  the endpoint could answer that everything was fine. A timing-out check no
  longer delays or cancels its siblings.

- **Database connections are recycled before a proxy closes them.** The engine
  was created without `pool_recycle`, so a pooled connection that pgbouncer or a
  cloud load balancer had silently dropped stayed in the pool until some
  unlucky request checked it out and failed with "server closed the connection
  unexpectedly". Now `POSTGRES__POOL_RECYCLE_SECONDS` (default 1800) — set it
  below the shortest idle timeout in front of your database.

- **`SqlAlchemyRepository` resolves its primary key once per model.**
  `_pk_column` ran `inspect()` on every access, so each `get()` and `delete()`
  re-derived an answer fixed at import time, and a repository built per request
  never got to reuse it.

- **`InMemoryRepository.get_list` accepts `order_by`.** The real repository had
  it and the fake did not, so the fake could not stand in for it in any test
  that ordered results. `order_by` is now part of the `Repository` contract.

- **Error responses now carry `X-Request-ID`, CORS and security headers.**
  Starlette runs the `Exception` handler in `ServerErrorMiddleware`, outside
  every user middleware, so 500 responses previously reached the client with
  none of them. A cross-origin SPA could not read the body at all — the browser
  turned it into an opaque CORS error, taking the request ID with it.
  `RequestContextMiddleware` now renders unhandled exceptions itself, inside the
  CORS and security-header layers.

- **Unhandled exceptions are logged once, and 500s appear in the access log.**
  Each 500 previously emitted two full tracebacks (middleware and handler), while
  the `request_completed` access-log line was skipped entirely — so dashboards
  counting status codes never saw a single 500 and error-rate alerts could not
  fire.

- **`apply_standard_middleware` ordering.** `SecurityHeadersMiddleware` now wraps
  `RequestContextMiddleware` instead of sitting inside it, so middleware-rendered
  error responses also get security headers.

- **Circuit breakers now open on connection failures.** `create_http_client`
  gated requests through httpx *response* event hooks, which never fire when
  there is no response — so a breaker counted 5xx only and stayed closed
  through connect refusals and timeouts, exactly the outage it exists to
  contain. Gating moved to `CircuitBreakerTransport`, which sees both. It wraps
  the retrying transport, so one logical request counts as one outcome however
  many connect retries it took. 4xx still leaves the breaker closed.

- **`redis_lock(auto_extend=True)` no longer leaks the lock.** The auto-extend
  task only caught `LockError`, so a `ConnectionError` killed it; reaping it in
  the `finally` block re-raised that error, which skipped `lock.release()` and
  replaced the guarded block's own exception with an infrastructure one. The
  lock then stayed held for the rest of its TTL, blocking every other worker.

- **Failed SQL queries are now logged, and query timings no longer leak.**
  `install_query_logger` pushed a timestamp per execution onto `conn.info` and
  popped it in `after_cursor_execute` — which never fires for a failing query.
  Entries accumulated for the whole life of each pooled connection and could
  mis-pair later measurements. Worse, deadlocks, statement timeouts and
  constraint violations were logged nowhere at all. Timing now lives on the
  per-execution context, and a `handle_error` listener emits `db_query_failed`
  regardless of `slow_query_threshold_ms`.

- **Access logs no longer trust a spoofable client address.** The access log
  parsed `X-Forwarded-For` unconditionally while the rate limiter used
  `request.client.host` — two different answers to "who is the caller" in one
  library, and the logged one could be forged by any client. Both now read the
  address the ASGI server resolved, through the shared
  `http.middleware.client_ip`.

- **Outbound gRPC calls are traced again.** `GrpcChannelPool` attached no OTel
  client interceptor, so channels sent no `traceparent` and a callee's spans
  started a brand-new trace instead of joining the caller's. Inbound calls were
  instrumented, which made the gap easy to miss: the trace broke at every
  service boundary. Disable with `GrpcChannelPool(use_otel_interceptor=False)`.

- **Request IDs propagate on streaming RPCs.** gRPC dispatches to a different
  interceptor class per RPC shape, and only the unary-unary one was registered,
  so unary-stream, stream-unary and stream-stream calls silently dropped the
  correlation ID.

- **Rate limiting no longer takes the API down when Redis does.** `hit()` raised
  straight through, so a Redis outage turned every rate-limited endpoint into a
  500. Both Redis limiters now fail open by default and set
  `RateLimitResult.degraded`; `fail_open=False` restores the strict behaviour.

- **`InMemoryRateLimiter` is bounded.** Its window map grew one entry per
  distinct key — per client IP in practice — and never shrank.

- **`RedisRateLimiter` no longer ships its Lua script on every request.** It
  uses `register_script` (EVALSHA) instead.

- **`create_redis` sets socket timeouts and health checks.** redis-py blocks
  forever by default, so a hung connection pinned an event-loop task; idle
  connections are now pinged before reuse, which is what stops a pool behind a
  load balancer handing out sockets the balancer already dropped.

### Changed

- **BREAKING (licensing) — pycommon is now MIT licensed.** It previously shipped
  under an "all rights reserved" proprietary LICENSE, and `pyproject.toml`
  declared `Proprietary`, which meant nobody outside the organisation had the
  right to use, fork or contribute to it regardless of the repository being
  visible. Both now say MIT, and the wheel carries `License-Expression: MIT`
  with the licence file bundled.

  *Migration:* none for existing consumers — this only grants rights, it does not
  withdraw any.

- **BREAKING — `apply_standard_middleware` no longer takes
  `content_security_policy` or `timeout_seconds`.** Both are read from
  `settings.http`, the way CORS always has been, so each value has exactly one
  source. Keeping an argument *and* a setting would have meant two places to
  look and a precedence rule to remember, which is the class of bug several
  earlier fixes on this branch were about.

  *Migration:*

  ```python
  # before
  apply_standard_middleware(app, settings, timeout_seconds=15)
  # after — in code
  apply_standard_middleware(app, settings.model_copy(update={"http": HttpSettings(timeout_seconds=15)}))
  # after — normally
  # HTTP__TIMEOUT_SECONDS=15 in the environment
  ```

  `metrics` remains an argument: it is structural, not environment-specific.

- **`SecurityHeadersMiddleware` HSTS options now come from settings** when
  installed through `apply_standard_middleware` (`HTTP__HSTS`,
  `HTTP__HSTS_MAX_AGE`). Constructing the middleware directly is unchanged.

- **BREAKING — `current_revision()` returns the revision instead of printing it.**
  It delegated to `alembic current`, which writes through Alembic's own output
  plumbing: the caller got `None` back and, depending on logging configuration,
  nothing on stdout either. It now returns `str | None` by reading the version
  table directly, which is what services logging their schema version at startup,
  deploy jobs asserting on it, and health endpoints reporting it actually need.

  *Migration:* callers relying on the printed output must log the return value
  themselves — `logger.info("schema", revision=current_revision(settings))`. It
  takes an optional `version_table` for services whose `env.py` configures a
  non-default one.

- **`aiohttp` 3.14.1 → 3.14.3 and `cryptography` 49.0.0 → 50.0.0** in the
  lockfile, closing four advisories (PYSEC-2026-3545/3546/3547 and
  PYSEC-2026-3552) that the new audit job found on its first run.

- **`pycommon.__version__` now comes from installed package metadata**
  (`importlib.metadata`) instead of a literal that had to be kept in step with
  `pyproject.toml` by hand. Imported from a source tree with nothing installed
  it reads `0.0.0+unknown` rather than a stale number.

- **BREAKING — `AsyncCircuitBreaker._before_call` / `_on_success` /
  `_on_failure` are now public** (`before_call`, `on_success`, `on_failure`).
  `pycommon.http` was already calling them across module boundaries; they are
  the supported way to drive a breaker from an integration that cannot use
  `call()` or the context manager.

- **BREAKING — validation errors and `HTTPException` now return
  `application/problem+json`.** Previously only `AppError` and unhandled
  exceptions used RFC 9457; everything else returned FastAPI's default
  `{"detail": ...}`. This affected pycommon's own dependencies: the 429 from
  `build_rate_limit_dep` and the 401/403 from `create_auth_deps` all used the
  non-standard shape, so the library broke the contract it defines.

  *Migration:* clients parsing `{"detail": ...}` for 4xx must read the Problem
  Details envelope instead. Validation failures keep their field-level detail —
  it moves from `detail` to the `errors` member:

  ```jsonc
  // before                          // after
  {"detail": [{"loc": ["body","n"],  {"type": "/problems/input",
               "type": "int_parsing", "title": "Input Error",
               "msg": "..."}]}        "status": 422,
                                      "detail": "Request validation failed",
                                      "error_code": 3,
                                      "errors": [{"loc": ["body","n"],
                                                  "type": "int_parsing",
                                                  "msg": "..."}]}
  ```

  `exc.headers` is preserved, so `WWW-Authenticate` on 401 and `Retry-After` on
  429 still reach the client.

- **BREAKING — unhandled exceptions no longer propagate out of the ASGI stack.**
  `RequestContextMiddleware` handles them and returns a 500. Tests relying on
  `TestClient(app)` raising must use `raise_server_exceptions=False` and assert
  on the response, or install the middleware with `handle_exceptions=False`.

### Added

- **`IdempotencyMiddleware`**, installed by passing `redis=` to
  `apply_standard_middleware`, with `HTTP__IDEMPOTENCY_TTL_SECONDS` (default one
  day). A client whose connection drops after the server committed cannot tell
  "created" from "not created"; the key is what lets it retry without creating a
  second order.

  Keys are scoped to `caller + method + path + key` — keys are chosen by clients,
  so two will eventually collide, and an unscoped store would hand the second
  client the first one's response. The body is fingerprinted, so reusing a key
  with different content returns 409 rather than silently discarding the second
  request. 5xx responses are not stored, because a stored server error makes the
  failure permanent for that key.

  It **fails closed** on a Redis outage, unlike the rate limiter and cache. Those
  degrade a convenience; this degrades the guarantee it exists to provide, and
  503 is safe for a client that is holding a key and can retry. `fail_open=True`
  where a duplicate is cheaper than a rejection.

- `ErrorCode.IDEMPOTENCY` (12) with a `/problems/idempotency` type at 409.

- **`BodySizeLimitMiddleware`** and `HTTP__MAX_BODY_BYTES`, off by default. A
  JSON endpoint with no limit buffers whatever it is sent, and `json.loads` on a
  gigabyte allocates several more — one request from one client can take the
  process down, with no volume required, which is what separates this from rate
  limiting.

  It checks twice, because either alone is insufficient: `Content-Length` is
  rejected before a byte of body is read, *and* the streamed bytes are counted
  regardless, since that header is optional under chunked transfer encoding and
  is in any case a claim by the caller. It is installed innermost, so the
  `BodyTooLarge` raised out of the handler's own `await request.body()` is caught
  before any layer that renders unhandled exceptions — otherwise an oversized
  body comes back as a 500 and the client is told the fault was ours.

- `ErrorCode.PAYLOAD_TOO_LARGE` (11) with a `/problems/payload-too-large` type at
  413, and `413` in `error_code_for_status`.

- **`HttpSettings` and `ServerSettings`**, nested on `BaseAppSettings` as `http`
  and `server` — every middleware knob that differs between deployments is now
  an environment variable rather than a function argument, so an operator
  changes a timeout without asking for a release. `HTTP__TIMEOUT_SECONDS`,
  `HTTP__CONTENT_SECURITY_POLICY`, `HTTP__HSTS`, `HTTP__HSTS_MAX_AGE`,
  `SERVER__HOST`, `SERVER__PORT`, `SERVER__FORWARDED_ALLOW_IPS`,
  `SERVER__DRAIN_DELAY_SECONDS`.

  They are on the base class rather than declared per service, unlike
  `DatabaseSettings` or `KeycloakSettings`, because every HTTP service using
  `apply_standard_middleware` or `run_uvicorn` needs them. Every default is off
  or safe: a timeout nobody chose would turn working slow endpoints into 504s on
  upgrade, and a CSP nobody chose would blank out `/docs`.

- `runtime.run_from_settings(app, settings.server)` — runs uvicorn from
  `ServerSettings`. The README previously showed
  `run_uvicorn(..., forwarded_allow_ips=settings.forwarded_allow_ips)`, naming a
  setting that did not exist; now it does.

- **`TimeoutMiddleware`** and `apply_standard_middleware(timeout_seconds=...)` —
  a per-request ceiling, off by default. A handler blocked on an upstream that
  never answers holds its connection, session and worker slot indefinitely;
  enough of them and the service serves nothing while every health check still
  passes. On expiry the handler is *cancelled*, not merely abandoned — answering
  504 while the work continues leaves the session checked out and the upstream
  call in flight, which is the same leak without the visibility.

  The clock covers **time-to-first-byte**, not the whole response: once a status
  line is produced the deadline is lifted, so SSE, downloads and streamed
  exports are unaffected. Installed innermost, so the 504 is counted by the
  metrics layer and carries a request ID. Probe and metrics paths are excluded by
  default.

- `ErrorCode.TIMEOUT` (10) with a `/problems/timeout` type at 504, and `504` in
  `error_code_for_status`.

- **Connection draining on shutdown.** `run_uvicorn(drain_delay_seconds=...)`
  keeps the server accepting traffic for that long after SIGTERM while
  `/health/ready` answers 503, so a load balancer can take the instance out of
  rotation *before* it stops listening. Kubernetes removes a pod from its
  endpoints and signals it concurrently, and the removal propagates
  asynchronously — without a drain, a pod stops accepting while traffic is still
  being routed to it, which is the connection-refused blip on every deploy. Off
  by default; must be shorter than `terminationGracePeriodSeconds`; raises if
  combined with `reload=True`, where a supervisor would swallow the signal and
  the setting would silently do nothing.

  `/health/live` deliberately keeps answering 200 while draining: a draining
  process is not broken, and a failing liveness probe would have the kubelet
  restart the container mid-shutdown, killing the in-flight requests the drain
  exists to protect. Readiness also skips its dependency checks while draining.

- `pycommon.begin_draining()` / `is_draining()` / `reset_draining()` and
  `runtime.DrainingServer` — the state is process-wide, so gRPC servicers,
  workers and custom probes can consult the same answer.

- **Integration tests for `ObjectStorageClient` against a real S3 server**
  (MinIO). The unit tests drive a fake client, which proves the wrapper calls the
  right methods with the right arguments but cannot prove those arguments are
  ones a server accepts. Addressing style, signature version and presigned URL
  construction are decided by botocore and only observable against something that
  answers like S3 — so the presigned URLs are now **fetched over HTTP** rather
  than merely inspected, in both the download and upload directions, and
  `use_path_style` is asserted to actually reach the wire.

- **Integration tests for telemetry against a real OTLP collector** (Jaeger).
  The existing tests assert control flow with the SDK mocked out, which proves
  the branches and nothing about whether spans leave the process — exporter
  configuration is exactly the thing that looks correct and silently drops
  everything. These export through `setup_telemetry` and then ask the collector
  what it received: that spans arrive, that `service.name` registers, and that a
  served request carries `http.request.id` on its span, which is the correlation
  claim the README makes and nothing verified. Each runs in a subprocess, because
  `setup_telemetry` installs a process-global provider that later calls reuse —
  in-process, the test would either contaminate the session or silently reuse
  someone else's provider and pass without exporting anything.

- **Integration tests for the Alembic helpers**, which had none: they need a
  database *and* a versions directory, so the one call a deploy job makes was the
  least exercised code in the library. Covers upgrade to head, idempotent
  re-runs (deploy jobs get retried), single-step downgrade, `current_revision`
  across all three states, the lifespan resource both skipping and running, and
  a real connection as a role whose password percent-encodes. They run against
  the stock `alembic init` `env.py` rather than a tuned one, so they test what a
  service actually has.

- **Integration tests against a real Postgres** (`tests/integration`), covering
  what SQLite cannot: that UUIDv7 primary keys round-trip through asyncpg's
  strict type handling, that `TimestampMixin` really produces timezone-aware
  values from the server clock, that the naming convention reaches the DDL
  Postgres actually creates, that the `handle_error` listener logs constraint
  violations *and statement timeouts* (neither has a SQLite equivalent), that
  `SqlAlchemyUnitOfWork` rolls back under real MVCC and uncommitted writes stay
  invisible to a second session, that cursor pagination survives a real `uuid`
  column, and that `pool_pre_ping` recovers a connection killed with
  `pg_terminate_backend` — the exact failure `pool_recycle` exists for. Skips
  unless `POSTGRES_TEST_DSN` is set.

- **Integration tests against a real Redis** (`tests/integration`), covering
  what `fakeredis` cannot: that the rate-limiter Lua scripts execute on Redis at
  all, that sliding-window scores come from server-side `TIME`, that fixed
  windows expire rather than sliding forward on every hit, that the lock is
  mutually exclusive and `auto_extend` outlives its TTL, and that stampede
  protection really collapses concurrent misses into one factory call. They skip
  unless `REDIS_TEST_URL` is set, so the default run stays offline; CI provides a
  service container. `make test-integration` runs them locally.

- **`paginate_offset` and `paginate_cursor`** (`pycommon.persistence`) — turn a
  SQLAlchemy `Select` into the `Page` envelope that already existed but that
  every service had to fill in by hand. They take a session rather than a
  request, so workers and CLI jobs can use them too. `limit` is clamped to
  `max_limit` (default 100) because it usually arrives from a query string, and
  an unbounded one asks the database for the whole table. `paginate_offset`
  takes `with_total=False` to skip the count query; `paginate_cursor` is keyset
  pagination and stays stable under concurrent inserts, where offset does not.
- **`UUIDv7PrimaryKeyMixin`, `TimestampMixin`, `SoftDeleteMixin`**
  (`pycommon.persistence`) — the three column sets nearly every service was
  copy-pasting. Timestamps default from the database clock, not the
  application's, so instances with drifting clocks cannot write rows that fail
  to order. `SoftDeleteMixin` exposes `is_active()` / `is_deleted()` predicates
  rather than installing a global query filter: a filter that applies itself to
  every query is one people forget exists, and the symptom is a report that
  quietly comes out short.
- **Coverage gate and dependency audit in CI.** `fail_under = 85` lives in
  `pyproject.toml`, so `make check` and CI enforce one number. A separate
  `audit` job runs `pip-audit` against the exported lockfile — the versions
  consumers actually resolve, not the loosest the constraints allow. It is a
  separate job because an advisory is news about the world rather than a defect
  in the pull request, and it should not mask a lint or test failure. `make
  audit` runs the same check locally.
- **Tests for the previously untested modules**: `ObjectStorageClient`
  (lifecycle, and that a 403 from `head_bucket` is not read as "missing, create
  it"), `setup_telemetry`/`shutdown_telemetry` control flow, and
  `create_engine_and_sessionmaker` pool wiring asserted on a real engine.
- CI now declares `permissions: contents: read` and cancels superseded runs for
  the same ref.
- `config.resolve_environment()` — the single resolution used by
  `resolve_env_files()`, `get_environment()` and `BaseAppSettings`.
- `logging.current_request_id()` — one reader for the request ID bound to the
  current context, working in HTTP handlers, outbound clients, gRPC servicers
  and workers alike.
- `SecurityHeadersMiddleware(content_security_policy=...)` and
  `apply_standard_middleware(content_security_policy=...)`, plus
  `API_CONTENT_SECURITY_POLICY` (`default-src 'none'; frame-ancestors 'none'`)
  for JSON-only services. Off by default: that policy blanks out Swagger UI and
  ReDoc, and a policy loose enough for them protects nothing — so shipping
  either as a default would silently break some services and silently fail to
  protect others.
- **RED metrics for HTTP and gRPC.** `pycommon.telemetry` set up traces only, so
  a service could be debugged one request at a time but not alerted on: no rate,
  no error ratio, no latency distribution. `setup_telemetry` now installs a
  `MeterProvider` alongside the tracer (same OTLP endpoint) and two layers record
  through it — `http.middleware.MetricsMiddleware`, on by default in
  `apply_standard_middleware`, and `runtime.MetricsServerInterceptor`, on by
  default in `GrpcServer`. Instruments come from the OTel *API*, so they stay
  free no-ops in a service that exports no metrics.

  Emitted: `http.server.request.duration`, `http.server.active_requests`,
  `rpc.server.duration`, with semconv attributes and bucket advisories. Series
  are labelled by the **route template**, never the raw path, and by a bounded
  method set — both are caller-controlled, and recording them verbatim lets any
  scanner mint unbounded cardinality in the metrics backend.
- `telemetry.setup_metrics` / `shutdown_metrics` — usable without a FastAPI app,
  for workers, gRPC servers and CLIs. `shutdown_telemetry` now flushes both
  providers.
- `telemetry.build_metrics_router` — Prometheus `/metrics` for clusters that
  scrape rather than receive OTLP, enabled by
  `setup_telemetry(prometheus_metrics=True)`. It answers 503 until metrics are
  initialized instead of serving an empty page a scraper would read as healthy.
- `OtelSettings.metrics_enabled`, `metrics_export_interval_ms`,
  `prometheus_enabled`, `prometheus_path`. Metrics are switchable separately
  from traces: sampling traces down is normal, but a sampled rate or error
  ratio means nothing.
- `apply_standard_middleware(metrics=...)`, `GrpcServer(use_metrics_interceptor=...)`.
- `DatabaseSettings.pool_recycle_seconds`, `pool_timeout_seconds`.
- `InMemoryRepository(default_order_by=...)`, and `order_by` on
  `Repository.get_list`. Ordering in the fake is by attribute name
  (`"-created_at"`); a SQLAlchemy expression raises rather than being ignored,
  because a silently unsorted page is a test that passes while asserting
  nothing.
- `ErrorCode.FORBIDDEN` (403), `NOT_FOUND` (404), `CONFLICT` (409),
  `RATE_LIMIT` (429), with matching `PROBLEM_TYPES` entries, `/problems/*` docs
  pages, and `AppError.forbidden()` / `.not_found()` / `.conflict()` /
  `.rate_limit()` factories.
- `errors.error_code_for_status()` — maps an HTTP status to an `ErrorCode`,
  returning `None` when no application code fits.
- `http.validation_exception_handler`, `http.http_exception_handler`, and
  `http.unhandled_problem_response` (builds the canonical 500 body from plain
  values, for pure-ASGI callers with no `Request` object).
- `RequestContextMiddleware(handle_exceptions=...)`.
- `problem_response(headers=...)`.
- `http.CircuitBreakerTransport` and `create_http_client(transport=...)` — swap
  the network layer for `httpx.MockTransport` in tests, or a custom transport.
- `http.middleware.client_ip(scope)` — one definition of the caller's address,
  shared by the access log and the rate-limit dependency.
- `run_uvicorn(forwarded_allow_ips=...)`, plus a "Deploying behind a proxy"
  section in the README. uvicorn's `127.0.0.1` default never matches a
  Kubernetes ingress pod, so until this is configured HSTS is never emitted and
  every anonymous caller shares one rate-limit bucket.

- **`cache.Cache` and the `@cached` decorator** — cache-aside for values and
  async functions, closing the gap where a module named `cache` could hold a
  lock and count requests but not actually cache anything. Takes no `Request`,
  so it works from gRPC servicers, Celery workers and CLI jobs as well as
  routes. Stampede protection (built on the existing `redis_lock`) is on by
  default, `ttl_seconds` is required rather than defaulted, and both reads and
  writes fail open. Ships `JsonSerializer` and `pydantic_serializer`.
- `cache.RedisSlidingWindowRateLimiter` — sliding window log, no burst at
  window boundaries. Scores come from Redis `TIME`, so instances with skewed
  clocks cannot corrupt a shared window, and denied requests are not recorded
  so a client hammering a closed limit cannot keep extending it.
- `cache.parse_rate` and `build_rate_limit_dep(limiter, "100/minute")` — also
  accepts `"10/15seconds"`, `"100 per 2 minutes"`, `"5/s"`.
- `X-RateLimit-Limit` / `-Remaining` / `-Reset` on every rate-limited response.
- `RedisSettings.socket_timeout_seconds`, `socket_connect_timeout_seconds`,
  `health_check_interval_seconds`, `retry_on_timeout`.
- `runtime.default_otel_client_interceptors` and
  `GrpcChannelPool(use_otel_interceptor=...)`, mirroring the server side.
- `RequestIdUnaryStreamClientInterceptor`,
  `RequestIdStreamUnaryClientInterceptor`,
  `RequestIdStreamStreamClientInterceptor`.

### Removed

- **BREAKING — `CelerySettings` and `MongoSettings` are gone.** Both were
  defined in `config/settings.py` and exported from `pycommon.config.__all__`,
  and no module in the library ever used them. Anyone reading the public API
  would reasonably conclude pycommon had Celery and Mongo support; it did not,
  and a settings class is not support. Configuration that configures nothing is
  worse than an absence, because an absence is at least honest about what you
  still have to write.

  *Migration:* move the class into the service that actually runs Celery or
  Mongo — the definitions were four and three fields of plain defaults, so this
  is a copy, not a rewrite:

  ```python
  class CelerySettings(BaseModel):
      broker_url: str = "redis://localhost:6379/1"
      result_backend: str = "redis://localhost:6379/2"
      task_always_eager: bool = False

  class MongoSettings(BaseModel):
      uri: str = "mongodb://localhost:27017"
      db: str = "app"
  ```

  Nested env keys (`CELERY__BROKER_URL`, `MONGO__URI`) keep working unchanged
  once the class is nested under the service's own settings.

  The OTel Celery and pymongo *instrumentors* stay in the `telemetry` extra.
  Those instrument a service's own Celery or pymongo when it has them, which is
  unrelated to whether this library ships settings for either.

- `http.middleware.request_context.FORWARDED_FOR_HEADER` — the middleware no
  longer parses that header (see above).

## [0.1.0]

Initial release.
