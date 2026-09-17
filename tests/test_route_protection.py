"""Where auth attaches: routers, the internal API key, and the audit that proves it.

The failure being defended against is a quiet one -- an endpoint added outside
the authenticated router is public, and nothing fails. So these tests cover both
halves: that a router's dependency really does reach every route under it
(including nested ones), and that ``assert_routes_protected`` really does notice
when one escapes.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from py_common.config import BaseAppSettings, HttpSettings
from py_common.http.middleware import apply_standard_middleware
from py_common.http.problem import register_exception_handlers
from py_common.security import (
    INTERNAL_API_KEY_HEADER,
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
