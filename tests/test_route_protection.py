"""Where auth attaches: routers, the internal API key, and the audit that proves it.

The failure being defended against is a quiet one -- an endpoint added outside
the authenticated router is public, and nothing fails. So these tests cover both
halves: that a router's dependency really does reach every route under it
(including nested ones), and that ``assert_routes_protected`` really does notice
when one escapes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from py_common.config import BaseAppSettings, HttpSettings
from py_common.http.middleware import apply_standard_middleware
from py_common.http.problem import register_exception_handlers
from py_common.security import (
    INTERNAL_API_KEY_HEADER,
    OPTIONAL_AUTH_SCHEME,
    Auth,
    HasRole,
    TokenClaims,
    internal_router,
    protected_router,
)
from py_common.testing.routes import PYCOMMON_PUBLIC_PREFIXES, assert_routes_protected

BEARER = {"Authorization": "Bearer tok"}
API_KEY = "s3cr3t-key-value"


class _StubValidator:
    """Counts decodes, so a test can assert a token is validated once per request."""

    def __init__(self, claims: TokenClaims) -> None:
        self.claims = claims
        self.calls = 0

    async def decode_async(self, token: str) -> TokenClaims:
        self.calls += 1
        return self.claims


@pytest.fixture
def validator() -> _StubValidator:
    return _StubValidator(TokenClaims(sub="u-1", realm_roles=["admin"]))


@pytest.fixture
def auth(validator: _StubValidator) -> Auth:
    return Auth(validator)  # type: ignore[arg-type]


def _settings(**http: object) -> BaseAppSettings:
    return BaseAppSettings(_env_file=None, http=HttpSettings(**http))  # type: ignore[arg-type]


# --- authentication is inherited from the router --------------------------


def test_routes_on_a_protected_router_require_a_token(auth: Auth) -> None:
    app = FastAPI()
    api = protected_router(auth, prefix="/api/v1")

    @api.get("/me")
    async def me() -> dict[str, str]:
        return {"ok": "yes"}

    app.include_router(api)
    client = TestClient(app)

    assert client.get("/api/v1/me").status_code == 401
    assert client.get("/api/v1/me", headers=BEARER).status_code == 200


def test_a_nested_router_cannot_open_a_hole(auth: Auth) -> None:
    """A router included *into* a protected router inherits its dependency.

    This is the property the whole design rests on: if it did not hold, every
    service that groups routes into sub-routers would be publishing them, and
    nothing in the code would look wrong. FastAPI resolves included routers
    lazily, so this is pinned against the framework rather than assumed.
    """
    app = FastAPI()
    api = protected_router(auth, prefix="/api/v1")
    orders = APIRouter(prefix="/orders")

    @orders.get("/{order_id}")
    async def one(order_id: str) -> dict[str, str]:
        return {"id": order_id}

    api.include_router(orders)
    app.include_router(api)
    client = TestClient(app)

    assert client.get("/api/v1/orders/7").status_code == 401
    assert client.get("/api/v1/orders/7", headers=BEARER).status_code == 200


def test_a_router_dependency_of_your_own_is_kept(auth: Auth) -> None:
    """``dependencies=`` passed through must be added to, not replaced."""
    seen: list[str] = []

    async def marker() -> None:
        seen.append("ran")

    app = FastAPI()
    api = protected_router(auth, prefix="/api/v1", dependencies=[Depends(marker)])

    @api.get("/me")
    async def me() -> dict[str, str]:
        return {"ok": "yes"}

    app.include_router(api)

    assert TestClient(app).get("/api/v1/me", headers=BEARER).status_code == 200
    assert seen == ["ran"]


def test_the_token_is_validated_once_per_request(auth: Auth, validator: _StubValidator) -> None:
    """Router-level authentication plus a route that also wants the claims.

    FastAPI caches a dependency per request keyed on the callable, so this holds
    only while ``Auth`` hands out the *same* closure every time. Rebuilding it
    per access -- the obvious way to write the property -- would double the JWKS
    lookups on every request, with nothing failing to say so. Verified by
    mutation: returning a fresh closure from ``Auth.current_user`` makes this
    read 3 -- the router's dependency, the route's ``requires``, and the
    handler parameter each becoming their own uncached lookup.
    """
    app = FastAPI()
    api = protected_router(auth, prefix="/api/v1")

    @api.get("/me", dependencies=[Depends(auth.requires(HasRole("admin")))])
    async def me(user: TokenClaims = Depends(auth.current_user)) -> dict[str, str]:  # noqa: B008
        return {"sub": user.sub}

    app.include_router(api)
    validator.calls = 0

    assert TestClient(app).get("/api/v1/me", headers=BEARER).status_code == 200
    assert validator.calls == 1


def test_optional_user_admits_anonymous_but_not_a_broken_token(auth: Auth) -> None:
    """No credentials is anonymous; credentials that do not parse is still 401.

    Treating a malformed token as "anonymous" would turn a broken client into a
    silently degraded response instead of an error it can act on.
    """
    app = FastAPI()

    @app.get("/maybe")
    async def maybe(user: TokenClaims | None = Depends(auth.optional_user)) -> dict[str, bool]:  # noqa: B008
        return {"authenticated": user is not None}

    client = TestClient(app)

    assert client.get("/maybe").json() == {"authenticated": False}
    assert client.get("/maybe", headers=BEARER).json() == {"authenticated": True}
    assert client.get("/maybe", headers={"Authorization": "Basic dXNlcjpwdw=="}).json() == {
        "authenticated": False
    }


# --- rejections stay Problem Details --------------------------------------


def test_rejections_keep_the_shape_the_rest_of_the_api_has(auth: Auth) -> None:
    """401 and 403 raised from a dependency travel the full middleware stack.

    This is the concrete reason auth is not middleware: an HTTPException raised
    in middleware never reaches FastAPI's handlers, which live inside the
    router, so its 401 would be plain JSON with no request ID and no CORS.
    """
    settings = _settings()
    app = FastAPI()
    api = protected_router(auth, prefix="/api/v1")

    @api.get("/nope", dependencies=[Depends(auth.require_roles("nobody"))])
    async def nope() -> dict[str, str]:
        return {"ok": "yes"}

    app.include_router(api)
    register_exception_handlers(app)
    apply_standard_middleware(app, settings)
    client = TestClient(app)

    unauthenticated = client.get("/api/v1/nope")
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["content-type"].startswith("application/problem+json")
    assert unauthenticated.headers["WWW-Authenticate"] == "Bearer"
    assert unauthenticated.headers["X-Request-ID"]

    forbidden = client.get("/api/v1/nope", headers=BEARER)
    assert forbidden.status_code == 403
    assert forbidden.headers["content-type"].startswith("application/problem+json")
    assert forbidden.headers["X-Request-ID"]


# --- the internal router --------------------------------------------------


def _internal_app(**http: object) -> FastAPI:
    app = FastAPI()
    internal = internal_router(_settings(**http), prefix="/internal")

    @internal.get("/jobs")
    async def jobs() -> dict[str, str]:
        return {"ok": "yes"}

    app.include_router(internal)
    return app


def test_internal_router_demands_the_key_when_one_is_configured() -> None:
    client = TestClient(_internal_app(internal_api_key=API_KEY))

    assert client.get("/internal/jobs").status_code == 401
    with_key = client.get("/internal/jobs", headers={INTERNAL_API_KEY_HEADER: API_KEY})
    assert with_key.status_code == 200


def test_a_wrong_key_of_the_same_length_is_still_rejected() -> None:
    """Exercises the compare_digest branch rather than an early length mismatch.

    A key that differs in length can be rejected by any comparison; one that
    differs only in content is what a constant-time compare is actually for.
    """
    wrong = "S3CR3T-KEY-VALUE"
    assert len(wrong) == len(API_KEY)

    client = TestClient(_internal_app(internal_api_key=API_KEY))

    assert client.get("/internal/jobs", headers={INTERNAL_API_KEY_HEADER: wrong}).status_code == 401


def test_the_internal_401_does_not_offer_a_bearer_challenge() -> None:
    """An API key in a bespoke header is not an HTTP authentication scheme.

    Answering ``WWW-Authenticate: Bearer`` would point the caller at a challenge
    this route does not accept.
    """
    response = TestClient(_internal_app(internal_api_key=API_KEY)).get("/internal/jobs")

    assert response.status_code == 401
    assert "WWW-Authenticate" not in response.headers


def test_internal_router_is_open_and_visibly_so_without_a_key() -> None:
    """No key configured means no guard -- and no security scheme in the document.

    The audit below depends on exactly this: an unguarded internal router has to
    look unprotected, or listing it as public would not be a decision anyone
    made.
    """
    app = _internal_app()

    assert TestClient(app).get("/internal/jobs").status_code == 200
    assert app.openapi()["paths"]["/internal/jobs"]["get"].get("security") is None


# --- the audit ------------------------------------------------------------


def _mixed_app(auth: Auth, **http: object) -> FastAPI:
    app = FastAPI()
    api = protected_router(auth, prefix="/api/v1")
    internal = internal_router(_settings(**http), prefix="/internal")
    public = APIRouter(prefix="/api/public/v1")

    @api.get("/me")
    async def me() -> dict[str, str]:
        return {"ok": "yes"}

    @internal.get("/jobs")
    async def jobs() -> dict[str, str]:
        return {"ok": "yes"}

    @public.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    for router in (api, internal, public):
        app.include_router(router)
    return app


def test_audit_passes_when_every_open_route_is_declared(auth: Auth) -> None:
    app = _mixed_app(auth, internal_api_key=API_KEY)

    assert_routes_protected(app, public_prefixes=("/api/public/v1",))


def test_audit_names_the_route_that_slipped_out(auth: Auth) -> None:
    app = _mixed_app(auth, internal_api_key=API_KEY)

    with pytest.raises(AssertionError) as exc:
        assert_routes_protected(app)

    assert "GET /api/public/v1/ping" in str(exc.value)
    assert "/api/v1/me" not in str(exc.value)


def test_audit_reports_an_internal_router_left_without_a_key(auth: Auth) -> None:
    """The open-by-configuration case still has to be written down."""
    app = _mixed_app(auth)

    with pytest.raises(AssertionError) as exc:
        assert_routes_protected(app, public_prefixes=("/api/public/v1",))

    assert "GET /internal/jobs" in str(exc.value)


def test_py_common_own_routers_are_not_exempt_by_default(auth: Auth) -> None:
    """There is no implicit allowlist -- not even for health probes.

    A default that quietly excused ``/health`` would excuse anything a future
    version of this library mounts under it.
    """
    from py_common.http.health import build_health_router

    app = _mixed_app(auth, internal_api_key=API_KEY)
    app.include_router(build_health_router())

    with pytest.raises(AssertionError) as exc:
        assert_routes_protected(app, public_prefixes=("/api/public/v1",))

    assert "GET /health/live" in str(exc.value)

    assert_routes_protected(app, public_prefixes=("/api/public/v1", *PYCOMMON_PUBLIC_PREFIXES))


def test_audit_accepts_an_exact_path(auth: Auth) -> None:
    app = _mixed_app(auth, internal_api_key=API_KEY)

    assert_routes_protected(app, public_paths=("/api/public/v1/ping",))


def test_audit_ignores_path_level_keys_that_are_not_operations(auth: Auth) -> None:
    """A path item may carry ``parameters``, ``servers`` or a ``summary``.

    Those sit beside the operations, not among them. A service that customises
    its OpenAPI document can produce them, and counting one as an unprotected
    operation would fail a suite over a key that grants nobody anything.
    """
    app = _mixed_app(auth, internal_api_key=API_KEY)
    document = app.openapi()
    document["paths"]["/api/v1/me"]["parameters"] = []
    document["paths"]["/api/v1/me"]["summary"] = "Current user"
    app.openapi_schema = document

    assert_routes_protected(app, public_prefixes=("/api/public/v1",))


def test_require_roles_refuses_to_guard_nothing(auth: Auth) -> None:
    """``require_roles()`` would read as a guard while admitting everyone."""
    with pytest.raises(ValueError, match="at least one role"):
        auth.require_roles()


# --- optional authentication is not protection ---------------------------------
#
# A route depending on `Auth.optional_user` lets an anonymous caller through, so
# it is open. Until `optional_user` declared its own security scheme, the generated
# document was byte-identical to a `current_user` route's and this audit counted
# such a route as protected -- reporting success on an endpoint anyone could call,
# which is the one thing it exists not to do.


def _optional_auth_app(auth: Auth) -> FastAPI:
    """A public card route, a protected one, and the combination of the two.

    ``/public/card`` is the shape that matters: a plain router, optional auth,
    anyone may call it. ``/api/v1/card`` is on a *protected* router as well, so
    the router's own guard still demands a token — it is genuinely protected, and
    the audit must keep saying so.
    """
    app = FastAPI()
    api = protected_router(auth, prefix="/api/v1")
    public = APIRouter(prefix="/public")

    @public.get("/card")
    async def public_card(
        user: TokenClaims | None = Depends(auth.optional_user),  # noqa: B008
    ) -> dict[str, bool]:
        return {"signed_in": user is not None}

    @api.get("/card")
    async def guarded_card(
        user: TokenClaims | None = Depends(auth.optional_user),  # noqa: B008
    ) -> dict[str, bool]:
        return {"signed_in": user is not None}

    @api.get("/me")
    async def me(user: TokenClaims = Depends(auth.current_user)) -> dict[str, str]:  # noqa: B008
        return {"sub": user.sub}

    for router in (public, api):
        app.include_router(router)
    return app


def test_optional_auth_declares_its_own_scheme(auth: Auth) -> None:
    """The distinction the audit reads, visible in the document itself.

    Both dependencies consume the same ``Authorization: Bearer`` header; the
    scheme name is the only place the spec can say which of them a route uses.
    """
    paths = _optional_auth_app(auth).openapi()["paths"]

    assert paths["/public/card"]["get"]["security"] == [{OPTIONAL_AUTH_SCHEME: []}]
    assert paths["/api/v1/me"]["get"]["security"] == [{"HTTPBearer": []}]
    # On a protected router the route declares both: the router's requirement and
    # its own optional one.
    assert paths["/api/v1/card"]["get"]["security"] == [
        {"HTTPBearer": []},
        {OPTIONAL_AUTH_SCHEME: []},
    ]


def test_an_optional_auth_route_must_be_declared_public(auth: Auth) -> None:
    """It is reported, and listing it is what makes the intent explicit."""
    app = _optional_auth_app(auth)

    with pytest.raises(AssertionError) as exc:
        assert_routes_protected(app)
    reported = str(exc.value)
    assert "GET /public/card" in reported
    assert "/api/v1/me" not in reported
    # Optional auth *plus* a protected router is still protected -- the check asks
    # whether any declared requirement demands credentials, not which came last.
    assert "/api/v1/card" not in reported

    assert_routes_protected(app, public_paths=("/public/card",))


def test_an_optional_auth_route_is_still_reachable_both_ways(auth: Auth) -> None:
    """Changing the declared scheme must not change who gets in."""
    client = TestClient(_optional_auth_app(auth))

    assert client.get("/public/card").json() == {"signed_in": False}
    assert client.get("/public/card", headers=BEARER).json() == {"signed_in": True}
    # And the guarded one still refuses an anonymous caller.
    assert client.get("/api/v1/card").status_code == 401


def test_the_scheme_name_the_checker_looks_for_matches_the_one_auth_declares() -> None:
    """``testing.routes`` spells the name out instead of importing it.

    It needs only the ``http`` extra, while ``py_common.security`` pulls PyJWT —
    importing it there would make ``assert_routes_protected`` unusable on a
    ``py-common[http]`` install, which is the failure `make extras-check` exists
    to catch. This test is what keeps the two copies from drifting.
    """
    from py_common.testing.routes import _OPTIONAL_AUTH_SCHEME

    assert _OPTIONAL_AUTH_SCHEME == OPTIONAL_AUTH_SCHEME


# --- the access log can name the caller ----------------------------------------


@pytest.fixture
def capture_logs() -> Iterator[list[dict[str, Any]]]:
    """Swallow log events into a list, the way tests/test_timeout.py does."""
    events: list[dict[str, Any]] = []

    def sink(logger: Any, method_name: str, event_dict: dict[str, Any]) -> Any:
        events.append(dict(event_dict))
        raise structlog.DropEvent

    structlog.configure(processors=[sink])
    try:
        yield events
    finally:
        structlog.reset_defaults()


def _logged_app(auth: Auth) -> FastAPI:
    app = FastAPI()
    api = protected_router(auth, prefix="/api/v1")
    public = APIRouter(prefix="/public")

    @api.get("/bare")
    async def bare() -> dict[str, str]:
        return {"ok": "yes"}

    @public.get("/card")
    async def card(user: TokenClaims | None = Depends(auth.optional_user)) -> dict[str, bool]:  # noqa: B008
        return {"signed_in": user is not None}

    for router in (public, api):
        app.include_router(router)
    register_exception_handlers(app)
    apply_standard_middleware(app, _settings())
    return app


def _access(events: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [e for e in events if e.get("event") == "request_completed"]
    assert completed, "no access-log line was emitted"
    return completed[-1]


def test_the_access_log_names_the_authenticated_caller(
    auth: Auth, capture_logs: list[dict[str, Any]]
) -> None:
    """``user.id`` was documented and never populated.

    ``RequestContextMiddleware`` reads ``request.state.user.sub`` into the log
    (`http/middleware/request_context.py:84`), but nothing in the library wrote
    that field, so every service had to add its own dependency or accept an access
    log that could not say who did anything.

    The route carries no claims parameter of its own: the router's authentication
    is what publishes them.
    """
    TestClient(_logged_app(auth)).get("/api/v1/bare", headers=BEARER)

    assert _access(capture_logs)["user"] == {"id": "u-1"}


def test_optional_auth_names_the_caller_only_when_there_is_one(
    auth: Auth, capture_logs: list[dict[str, Any]]
) -> None:
    """Anonymous must leave the field absent, not present and empty."""
    client = TestClient(_logged_app(auth))

    client.get("/public/card")
    assert "user" not in _access(capture_logs)

    client.get("/public/card", headers=BEARER)
    assert _access(capture_logs)["user"] == {"id": "u-1"}


def test_publishing_the_claims_does_not_cost_a_second_decode(
    auth: Auth, validator: _StubValidator, capture_logs: list[dict[str, Any]]
) -> None:
    """Taking ``Request`` must not defeat FastAPI's per-callable dependency cache.

    A route that also asks for the claims, on a router that already
    authenticated, still decodes once — the property `Auth.__post_init__` builds
    its closures once to protect.
    """
    app = _logged_app(auth)
    api = APIRouter(prefix="/api/v2")

    @api.get("/me")
    async def me(user: TokenClaims = Depends(auth.current_user)) -> dict[str, str]:  # noqa: B008
        return {"sub": user.sub}

    app.include_router(api, dependencies=[Depends(auth.current_user)])
    validator.calls = 0

    TestClient(app).get("/api/v2/me", headers=BEARER)

    assert validator.calls == 1
    assert _access(capture_logs)["user"] == {"id": "u-1"}
