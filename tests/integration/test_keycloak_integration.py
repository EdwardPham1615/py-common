"""The security module against a real Keycloak.

Everything in ``tests/test_security.py`` is a closed loop. The JWKS client is a
``MagicMock``, so the configured ``jwks_url`` is never fetched; the discovery
document is one we wrote ourselves; the tokens are signed by our own keypair
with ``issuer`` and ``audience`` chosen to be what the validator already
expects. That suite proves the Python is coherent — cache lifetimes, the
refresh-once path on an unknown ``kid``, the RBAC predicates. It cannot prove a
single thing about Keycloak, because Keycloak is never in the room.

These tests put it in the room. They exist to answer the questions a
self-signed token cannot: is the JWKS path we construct the path Keycloak
serves, is the ``iss`` string what we assume, do roles land where we look for
them, and — the one that turned out to matter most — what is actually in
``aud``.

The realm lives in ``keycloak-realm.json`` beside this file and is imported at
container start, so there is no setup step: ``make infra-up`` is all of it. It
holds two clients that differ by exactly one thing, a dedicated audience
mapper, because that difference is what the audience tests below are built on.
Keycloak rejects unknown JSON properties, so the file carries no comments of
its own — this docstring and README's "Working on the Keycloak fixture" are
where it is explained.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import jwt
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from jwt import PyJWKClient

from py_common.config import BaseAppSettings
from py_common.http.middleware import apply_standard_middleware
from py_common.http.problem import register_exception_handlers
from py_common.security import (
    Auth,
    ClientCredentialsTokenProvider,
    HasRole,
    HasScope,
    KeycloakTokenValidator,
    TokenClaims,
    protected_router,
)

pytestmark = pytest.mark.integration

TokenFetcher = Callable[..., dict[str, Any]]


# --- the URLs we construct ------------------------------------------------


async def test_discovery_agrees_with_every_url_we_build(keycloak_settings: Any) -> None:
    """The highest-value test here: our string-building against the server's own answer.

    ``KeycloakSettings`` derives ``issuer``, ``jwks_url`` and ``token_url`` by
    concatenation. Every unit test that checks them compares one of our
    constants to another of our constants — including the one that asserts the
    discovery document's ``jwks_uri`` equals ``KC.jwks_url``, where we wrote the
    document. Nothing in the offline suite would notice if Keycloak moved a
    path or emitted a different ``iss``; a service would notice, in production,
    as a 401 with no explanation.
    """
    config = await KeycloakTokenValidator(settings=keycloak_settings).fetch_openid_config()

    assert config["issuer"] == keycloak_settings.issuer
    assert config["jwks_uri"] == keycloak_settings.jwks_url
    assert config["token_endpoint"] == keycloak_settings.token_url


def test_the_jwks_url_really_serves_the_signing_key(
    keycloak_settings: Any, keycloak_token: TokenFetcher
) -> None:
    """A real fetch, not an injected mock, and a key that matches a real token."""
    token = keycloak_token("pycommon-api", user="alice")["access_token"]

    signing_key = PyJWKClient(keycloak_settings.jwks_url).get_signing_key_from_jwt(token)

    assert signing_key.key is not None


# --- what a real token contains -------------------------------------------


def test_a_real_user_token_decodes_and_its_roles_are_where_we_look(
    keycloak_settings: Any, keycloak_token: TokenFetcher
) -> None:
    """Realm roles, client roles and scopes, read off a token Keycloak minted.

    ``realm_access.roles`` and ``resource_access.<client_id>.roles`` are the two
    paths the validator reads. Until now the only evidence they are right was
    that our own token factory writes to them.
    """
    token = keycloak_token("pycommon-api", user="alice")["access_token"]

    claims = KeycloakTokenValidator(settings=keycloak_settings).decode(token)

    assert claims.preferred_username == "alice"
    assert claims.email == "alice@example.test"
    assert claims.realm_roles == ["admin"]
    assert claims.client_roles == ["orders:write"]
    assert claims.roles == {"admin", "orders:write"}
    # Keycloak sends `scope` as one space-separated string; this is the split
    # working against the real thing rather than against our own factory.
    #
    # As a set, because Keycloak does not promise an order and does not keep
    # one -- the same realm answered "profile email" locally and "email
    # profile" in CI. `HasScope` intersects sets, so order is not something
    # this library relies on, and a test that pinned it would only be a flake
    # waiting for the next run.
    assert set(claims.scopes) == {"profile", "email"}


# --- audience: what it actually means --------------------------------------
#
# These two record behaviour that is easy to assume wrongly, and that we did
# assume wrongly before running it. Keycloak's default audience-resolve mapper
# fills `aud` from the clients the SUBJECT HOLDS ROLES ON -- not from the client
# that requested the token, which is `azp`.


def test_aud_names_our_client_only_because_the_user_holds_a_role_there(
    keycloak_settings: Any, keycloak_token: TokenFetcher
) -> None:
    """A token requested by a *different* client passes our audience check.

    alice asks ``pycommon-api-noaud`` — a client with no audience mapper — for a
    token, and our validator, configured for ``pycommon-api`` with
    ``verify_aud=True``, accepts it. It is not a bug in the validator: `aud`
    genuinely contains ``pycommon-api``, because alice holds a role there.

    The consequence is worth stating plainly, because ``.env.example`` said the
    opposite until this test was written: **``verify_aud=True`` does not
    establish that the token was issued to your client.** Any other client in
    the realm can obtain a token your API will accept, for any user who has a
    role on your API. ``azp`` is the claim that names the requesting client, and
    nothing in py_common checks it today.
    """
    token = keycloak_token("pycommon-api-noaud", user="alice")["access_token"]

    claims = KeycloakTokenValidator(settings=keycloak_settings).decode(token)

    assert claims.raw["aud"] == "pycommon-api"
    assert claims.raw["azp"] == "pycommon-api-noaud"
    assert keycloak_settings.verify_aud is True


def test_a_subject_with_no_role_on_our_client_gets_aud_account_and_is_rejected(
    keycloak_settings: Any, keycloak_token: TokenFetcher
) -> None:
    """The other half: no role on our client, so `aud` falls back to ``account``.

    This is the failure a service meets on day one against a realm nobody has
    tuned — a perfectly valid token rejected as "Invalid or expired token",
    with nothing in the message pointing at the audience. Recorded here so the
    next person recognises it in a minute rather than an afternoon.
    """
    token = keycloak_token("pycommon-api-noaud")["access_token"]

    unverified = jwt.decode(token, options={"verify_signature": False})
    assert unverified["aud"] == "account"

    with pytest.raises(HTTPException) as rejected:
        KeycloakTokenValidator(settings=keycloak_settings).decode(token)

    assert rejected.value.status_code == 401
    assert rejected.value.detail == "Invalid or expired token"


def test_allowed_azp_closes_the_hole_the_audience_check_leaves_open(
    keycloak_settings: Any, keycloak_token: TokenFetcher
) -> None:
    """The same token, the same validator, one setting apart.

    This is the test above turned around. alice's token from
    ``pycommon-api-noaud`` passes an audience check it arguably should not,
    because Keycloak put ``pycommon-api`` in ``aud`` on the strength of her
    roles. ``allowed_azp`` is what lets a service say it trusts tokens from its
    own front end and not from every client sharing the realm — and here it
    rejects the very token the previous test proved gets through.
    """
    token = keycloak_token("pycommon-api-noaud", user="alice")["access_token"]

    permissive = KeycloakTokenValidator(settings=keycloak_settings)
    assert permissive.decode(token).raw["azp"] == "pycommon-api-noaud"

    strict = KeycloakTokenValidator(
        settings=keycloak_settings.model_copy(update={"allowed_azp": ["pycommon-api"]})
    )
    with pytest.raises(HTTPException) as rejected:
        strict.decode(token)
    assert rejected.value.detail == "Token was not issued to a client this service accepts"

    # And the client's own token still gets through the strict validator, so
    # this is narrowing rather than breaking.
    own = keycloak_token("pycommon-api", user="alice")["access_token"]
    assert strict.decode(own).raw["azp"] == "pycommon-api"


# --- service-to-service ----------------------------------------------------


async def test_client_credentials_round_trips_through_our_own_validator(
    keycloak_settings: Any,
) -> None:
    """The provider fetches a token; the validator accepts it. Both sides ours.

    Closes the question PR #49 left open when it declined to send ``scope`` with
    the grant: a service-account token carries the client roles of its service
    account, so ``HasRole`` is what authorises service-to-service calls today.
    """
    provider = ClientCredentialsTokenProvider(settings=keycloak_settings)

    token = await provider.get_token()
    claims = KeycloakTokenValidator(settings=keycloak_settings).decode(token)

    assert claims.sub
    assert claims.client_roles == ["orders:write"]
    # No realm roles on a service account, and no scope was requested -- so
    # roles are the only thing an authorization rule has to work with here.
    assert claims.realm_roles == []


# --- the layer that sits on top of the validator ---------------------------
#
# Everything above exercises KeycloakTokenValidator. The routers and
# requirements built on it were only ever tested against a stub validator, so
# nothing proved that a real bearer header travels all the way through: header
# -> signature and audience check -> claims -> predicate -> status code. These
# do, through the real middleware stack, so the answer to "does our auth work
# against Keycloak" stops being something a person has to assemble by hand.


@pytest.fixture
def auth_app(keycloak_settings: Any) -> FastAPI:
    """A service-shaped app: authentication on the router, authorization on routes."""
    auth = Auth(KeycloakTokenValidator(settings=keycloak_settings))
    settings = BaseAppSettings(_env_file=None)

    app = FastAPI()
    api = protected_router(auth, prefix="/api/v1")

    # Carries nothing of its own: no claims parameter, no route dependency.
    # Every other route here reaches current_user through its own chain, so this
    # is the only one whose protection comes purely from the router. Without it,
    # removing the router's dependency entirely would fail no test -- which is
    # how this route came to exist.
    @api.get("/bare")
    async def bare() -> dict[str, str]:
        return {"ok": "yes"}

    @api.get("/me")
    async def me(user: TokenClaims = Depends(auth.current_user)) -> dict[str, Any]:  # noqa: B008
        return {"sub": user.sub, "roles": sorted(user.roles), "scopes": sorted(user.scopes)}

    @api.get("/admin", dependencies=[Depends(auth.require_roles("admin"))])
    async def admin() -> dict[str, str]:
        return {"ok": "yes"}

    # Guarded by a CLIENT role alone. Without it, every passing route here could
    # be satisfied by alice's realm role, and nothing would prove that roles
    # under resource_access reach a rule at all.
    @api.get("/orders-write", dependencies=[Depends(auth.require_roles("orders:write"))])
    async def orders_write() -> dict[str, str]:
        return {"ok": "yes"}

    @api.get("/nobody", dependencies=[Depends(auth.require_roles("nobody"))])
    async def nobody() -> dict[str, str]:
        return {"ok": "yes"}

    @api.post(
        "/orders",
        dependencies=[Depends(auth.requires(HasRole("admin") | HasScope("orders:write")))],
    )
    async def orders() -> dict[str, str]:
        return {"ok": "yes"}

    @api.post("/scope-only", dependencies=[Depends(auth.requires(HasScope("orders:write")))])
    async def scope_only() -> dict[str, str]:
        return {"ok": "yes"}

    app.include_router(api)
    register_exception_handlers(app)
    apply_standard_middleware(app, settings)
    return app


@pytest.fixture
def alice_headers(keycloak_token: TokenFetcher) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {keycloak_token('pycommon-api', user='alice')['access_token']}"
    }


def test_a_protected_router_admits_a_real_token_and_nothing_else(
    auth_app: FastAPI, alice_headers: dict[str, str]
) -> None:
    """The whole path, end to end, for the first time.

    The offline equivalent stubs out decoding entirely, so it proves the wiring
    and not that a token Keycloak actually issued gets through it.
    """
    client = TestClient(auth_app)

    # The route with nothing of its own: only the router stands between an
    # anonymous caller and the handler.
    assert client.get("/api/v1/bare").status_code == 401
    assert client.get("/api/v1/bare", headers=alice_headers).status_code == 200

    assert client.get("/api/v1/me").status_code == 401
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401

    body = client.get("/api/v1/me", headers=alice_headers).json()
    assert body["roles"] == ["admin", "orders:write"]
    assert body["sub"]


def test_role_rules_decide_on_the_roles_keycloak_really_sent(
    auth_app: FastAPI, alice_headers: dict[str, str]
) -> None:
    """A realm role and a client role, reaching the predicates as one set.

    alice holds ``admin`` in ``realm_access`` and ``orders:write`` under
    ``resource_access``. Both have to arrive for ``HasRole`` to mean what the
    README says it means.
    """
    client = TestClient(auth_app)

    # The realm role, then the client role on its own -- the second is what
    # proves resource_access is reaching the predicate and not merely the
    # claims object.
    assert client.get("/api/v1/admin", headers=alice_headers).status_code == 200
    assert client.get("/api/v1/orders-write", headers=alice_headers).status_code == 200
    assert client.post("/api/v1/orders", headers=alice_headers).status_code == 200

    denied = client.get("/api/v1/nobody", headers=alice_headers)
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Insufficient permissions; requires: role:nobody"


def test_rejections_are_problem_details_all_the_way_through(
    auth_app: FastAPI, alice_headers: dict[str, str]
) -> None:
    """401 and 403 keep the shape every other error in a service has.

    This is the property that made auth a dependency rather than middleware, now
    checked against a real token instead of a stubbed decode.
    """
    client = TestClient(auth_app)

    unauthenticated = client.get("/api/v1/me")
    assert unauthenticated.headers["content-type"].startswith("application/problem+json")
    assert unauthenticated.headers["WWW-Authenticate"] == "Bearer"
    assert unauthenticated.headers["X-Request-ID"]

    forbidden = client.get("/api/v1/nobody", headers=alice_headers)
    assert forbidden.headers["content-type"].startswith("application/problem+json")
    assert forbidden.headers["X-Request-ID"]


async def test_a_requested_scope_reaches_a_has_scope_rule(
    keycloak_settings: Any,
) -> None:
    """The path ``KEYCLOAK__TOKEN_SCOPE`` exists to open, end to end.

    ``phone`` rather than a business scope like ``orders:write``, and the choice
    is worth explaining: declaring ``clientScopes`` in a realm import
    **replaces Keycloak's entire built-in set**, not adds to it. Doing that here
    wiped ``profile``, ``email`` and — fatally — ``roles``, so tokens came back
    with no ``realm_access`` at all. Keeping the defaults would mean
    transplanting every one of them, with their mappers, into the fixture: the
    thousand-line file this realm is hand-written to avoid being. ``phone`` is
    an optional scope Keycloak already assigns to every client, so it exercises
    exactly the same mechanism for free. README's "Scoping a service token"
    documents the real setup, including this trap.
    """
    scoped = keycloak_settings.model_copy(update={"token_scope": "phone"})
    validator = KeycloakTokenValidator(settings=scoped)

    unscoped_claims = validator.decode(
        await ClientCredentialsTokenProvider(settings=keycloak_settings).get_token()
    )
    scoped_claims = validator.decode(
        await ClientCredentialsTokenProvider(settings=scoped).get_token()
    )

    assert not HasScope("phone").check(unscoped_claims)
    assert HasScope("phone").check(scoped_claims)
    # The defaults are still there -- asking for one scope does not narrow away
    # the rest, so nothing an existing rule relies on is lost.
    assert {"profile", "email"} <= set(scoped_claims.scopes)


def test_has_scope_denies_a_scope_the_caller_never_requested(
    auth_app: FastAPI, alice_headers: dict[str, str], keycloak_url: str
) -> None:
    """The other half: a rule naming a scope nobody asked for denies, as it should.

    alice's browser-style token carries only ``profile email``, so
    ``HasScope("orders:write")`` refuses a caller who is otherwise entitled --
    the same request passes under ``HasRole``, and the combined
    ``HasRole | HasScope`` route above passes through the role branch.

    And she cannot conjure it: Keycloak answers ``invalid_scope`` for a scope no
    client scope defines. That is the constraint ``KEYCLOAK__TOKEN_SCOPE``
    works within rather than around — it asks for scopes the realm already
    grants this client, which is the whole security value of the mechanism.
    """
    client = TestClient(auth_app)

    denied = client.post("/api/v1/scope-only", headers=alice_headers)
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Insufficient permissions; requires: scope:orders:write"

    refused = httpx.post(
        f"{keycloak_url}/realms/pycommon-test/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "pycommon-api",
            "client_secret": "pycommon-api-secret",
            "username": "alice",
            "password": "alice-password",
            "scope": "orders:write",
        },
        timeout=10.0,
    )
    assert refused.status_code == 400
    assert refused.json()["error"] == "invalid_scope"
