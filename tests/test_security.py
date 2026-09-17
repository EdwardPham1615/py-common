"""Keycloak token validation, the Auth dependencies, and service-to-service tokens."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qsl

import anyio
import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from pycommon.config import KeycloakSettings
from pycommon.security import (
    Auth,
    ClientCredentialsTokenProvider,
    KeycloakTokenValidator,
    TokenClaims,
)
from pycommon.testing.tokens import RsaKeyPair, generate_rsa_keypair, issue_test_token


@contextlib.contextmanager
def _patched_async_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Iterator[None]:
    """Answer every outbound httpx request from ``handler``.

    The code under test builds its own ``httpx.AsyncClient`` inside the method,
    so there is no client to inject -- swapping the class for the duration is
    what lets these tests drive Keycloak's side of the exchange.
    """
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(handler))

    with patch("httpx.AsyncClient", factory):
        yield


KC = KeycloakSettings(server_url="http://kc:8080", realm="test", client_id="test-api")
KC_CONFIDENTIAL = KeycloakSettings(
    server_url="http://kc:8080", realm="test", client_id="svc-api", client_secret="s3cret"
)


@pytest.fixture(scope="module")
def keypair() -> RsaKeyPair:
    return generate_rsa_keypair()


def _validator_with_key(keypair: RsaKeyPair) -> KeycloakTokenValidator:
    """Validator whose JWKS client returns our test public key without network I/O."""
    validator = KeycloakTokenValidator(settings=KC)
    signing_key = MagicMock()
    signing_key.key = keypair.public_pem
    jwks_client = MagicMock()
    jwks_client.get_signing_key_from_jwt.return_value = signing_key
    validator._jwks_client = jwks_client
    validator._jwks_fetched_at = float("inf")  # never expires during the test
    return validator


def test_decode_valid_token(keypair: RsaKeyPair) -> None:
    token = issue_test_token(
        keypair,
        issuer=KC.issuer,
        audience=KC.client_id,
        sub="user-1",
        realm_roles=["admin"],
        client_roles={"test-api": ["orders:write"]},
    )
    claims = _validator_with_key(keypair).decode(token)
    assert claims.sub == "user-1"
    assert claims.realm_roles == ["admin"]
    assert claims.client_roles == ["orders:write"]


def test_expired_token_rejected_without_jwks_refresh(keypair: RsaKeyPair) -> None:
    """Expired tokens must 401 immediately — NOT trigger a JWKS refetch."""
    token = issue_test_token(
        keypair, issuer=KC.issuer, audience=KC.client_id, expires_in_seconds=-60
    )
    validator = _validator_with_key(keypair)
    with (
        patch.object(validator, "_get_jwks_client", wraps=validator._get_jwks_client) as spy,
        pytest.raises(HTTPException) as exc_info,
    ):
        validator.decode(token)

    assert exc_info.value.status_code == 401
    refresh_calls = [c for c in spy.call_args_list if c.kwargs.get("force_refresh")]
    assert refresh_calls == [], "expired token must not force a JWKS refresh"


def test_wrong_audience_rejected(keypair: RsaKeyPair) -> None:
    token = issue_test_token(keypair, issuer=KC.issuer, audience="other-service")
    with pytest.raises(HTTPException) as exc_info:
        _validator_with_key(keypair).decode(token)
    assert exc_info.value.status_code == 401


def test_wrong_issuer_rejected(keypair: RsaKeyPair) -> None:
    token = issue_test_token(keypair, issuer="http://evil/realms/test", audience=KC.client_id)
    with pytest.raises(HTTPException) as exc_info:
        _validator_with_key(keypair).decode(token)
    assert exc_info.value.status_code == 401


def test_unknown_kid_triggers_single_refresh(keypair: RsaKeyPair) -> None:
    """Key rotation (PyJWKClientError) refreshes JWKS exactly once, then succeeds."""
    from jwt.exceptions import PyJWKClientError

    token = issue_test_token(keypair, issuer=KC.issuer, audience=KC.client_id)
    validator = KeycloakTokenValidator(settings=KC)

    signing_key = MagicMock()
    signing_key.key = keypair.public_pem
    good_jwks = MagicMock()
    good_jwks.get_signing_key_from_jwt.return_value = signing_key
    stale_jwks = MagicMock()
    stale_jwks.get_signing_key_from_jwt.side_effect = PyJWKClientError("unknown kid")

    calls: list[bool] = []

    def fake_get(*, force_refresh: bool = False) -> Any:
        calls.append(force_refresh)
        return good_jwks if force_refresh else stale_jwks

    with patch.object(validator, "_get_jwks_client", side_effect=fake_get):
        claims = validator.decode(token)

    assert claims.sub == "test-user"
    assert calls == [False, True]


async def test_decode_async(keypair: RsaKeyPair) -> None:
    token = issue_test_token(keypair, issuer=KC.issuer, audience=KC.client_id)
    claims = await _validator_with_key(keypair).decode_async(token)
    assert claims.sub == "test-user"


# --- JWKS client lifetime -------------------------------------------------
#
# The validator caches a PyJWKClient and rebuilds it when the TTL lapses. Both
# halves matter: rebuilding per call would fetch the key set on every request,
# and never rebuilding would keep serving a key set Keycloak has rotated away
# from until the process restarts.


def test_jwks_client_is_reused_within_the_ttl() -> None:
    validator = KeycloakTokenValidator(settings=KC)

    with patch("pycommon.security.keycloak.PyJWKClient") as client_cls:
        first = validator._get_jwks_client()
        second = validator._get_jwks_client()

    assert first is second
    assert client_cls.call_count == 1
    assert client_cls.call_args.args[0] == KC.jwks_url
    assert client_cls.call_args.kwargs["lifespan"] == KC.jwks_cache_ttl_seconds


def test_jwks_client_is_rebuilt_after_the_ttl() -> None:
    validator = KeycloakTokenValidator(settings=KeycloakSettings(**{**KC.model_dump()}))

    with patch("pycommon.security.keycloak.PyJWKClient") as client_cls:
        client_cls.side_effect = [MagicMock(name="first"), MagicMock(name="second")]
        first = validator._get_jwks_client()
        # Pretend the cache was populated longer ago than the TTL allows.
        validator._jwks_fetched_at -= KC.jwks_cache_ttl_seconds + 1
        second = validator._get_jwks_client()

    assert first is not second
    assert client_cls.call_count == 2


def test_forced_refresh_rebuilds_even_inside_the_ttl() -> None:
    validator = KeycloakTokenValidator(settings=KC)

    with patch("pycommon.security.keycloak.PyJWKClient") as client_cls:
        client_cls.side_effect = [MagicMock(name="first"), MagicMock(name="second")]
        first = validator._get_jwks_client()
        second = validator._get_jwks_client(force_refresh=True)

    assert first is not second


# --- openid configuration -------------------------------------------------


async def test_fetch_openid_config_returns_the_document() -> None:
    validator = KeycloakTokenValidator(settings=KC)
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(200, json={"issuer": KC.issuer, "jwks_uri": KC.jwks_url})

    with _patched_async_client(handler):
        config = await validator.fetch_openid_config()

    assert config["jwks_uri"] == KC.jwks_url
    assert captured == [KC.openid_config_url]


async def test_fetch_openid_config_raises_on_error_status() -> None:
    validator = KeycloakTokenValidator(settings=KC)

    with (
        _patched_async_client(lambda _: httpx.Response(503)),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await validator.fetch_openid_config()


# --- RBAC dependencies ----------------------------------------------------
#
# "Fail closed on authorization" is a stated design rule, and nothing exercised
# these two dependencies until now. They are the only thing standing between a
# caller and a handler.


def _auth_app(keypair: RsaKeyPair) -> FastAPI:
    auth = Auth(_validator_with_key(keypair))
    app = FastAPI()

    # Depends() in a parameter default is FastAPI's own API (the reason
    # pyproject already exempts src/pycommon/security from B008). Only /me needs
    # the claims object; the role-gated routes assert on status codes, so they
    # take the dependency the way tests/test_cache.py does.
    @app.get("/me")
    async def me(user: TokenClaims = Depends(auth.current_user)) -> dict[str, str]:  # noqa: B008
        return {"sub": user.sub}

    @app.get("/admin", dependencies=[Depends(auth.require_roles("admin"))])
    async def admin() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/both", dependencies=[Depends(auth.require_roles("admin", "auditor", any_of=False))])
    async def both() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/billing", dependencies=[Depends(auth.require_roles("billing"))])
    async def billing() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def _bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_missing_credentials_are_rejected(keypair: RsaKeyPair) -> None:
    response = TestClient(_auth_app(keypair)).get("/me")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["detail"] == "Not authenticated"


def test_non_bearer_scheme_is_rejected(keypair: RsaKeyPair) -> None:
    response = TestClient(_auth_app(keypair)).get(
        "/me", headers={"Authorization": "Basic dXNlcjpwYXNz"}
    )

    assert response.status_code == 401


def test_valid_token_reaches_the_handler(keypair: RsaKeyPair) -> None:
    token = issue_test_token(keypair, issuer=KC.issuer, audience=KC.client_id, sub="user-7")
    response = TestClient(_auth_app(keypair)).get("/me", headers=_bearer_headers(token))

    assert response.status_code == 200
    assert response.json() == {"sub": "user-7"}


def test_invalid_token_is_rejected_by_the_dependency(keypair: RsaKeyPair) -> None:
    expired = issue_test_token(
        keypair, issuer=KC.issuer, audience=KC.client_id, expires_in_seconds=-10
    )
    response = TestClient(_auth_app(keypair)).get("/me", headers=_bearer_headers(expired))

    assert response.status_code == 401


def test_scopes_are_split_out_of_the_standard_scope_claim(keypair: RsaKeyPair) -> None:
    """RFC 6749 makes ``scope`` a single space-separated string, not a list.

    Carrying it through unsplit would make ``HasScope`` match only a caller
    whose entire scope string equalled the one value asked for -- which is to
    say, almost nobody, and with nothing failing to explain why.
    """
    token = issue_test_token(
        keypair,
        issuer=KC.issuer,
        audience=KC.client_id,
        scopes=["openid", "orders:write"],
    )
    claims = _validator_with_key(keypair).decode(token)

    assert claims.scopes == ["openid", "orders:write"]


def test_a_token_without_scopes_yields_none_rather_than_failing(keypair: RsaKeyPair) -> None:
    token = issue_test_token(keypair, issuer=KC.issuer, audience=KC.client_id)

    assert _validator_with_key(keypair).decode(token).scopes == []


def test_realm_role_grants_access(keypair: RsaKeyPair) -> None:
    token = issue_test_token(
        keypair, issuer=KC.issuer, audience=KC.client_id, realm_roles=["admin"]
    )
    response = TestClient(_auth_app(keypair)).get("/admin", headers=_bearer_headers(token))

    assert response.status_code == 200


def test_client_role_grants_access(keypair: RsaKeyPair) -> None:
    """Roles under resource_access[client_id] count the same as realm roles."""
    token = issue_test_token(
        keypair,
        issuer=KC.issuer,
        audience=KC.client_id,
        client_roles={KC.client_id: ["billing"]},
    )
    response = TestClient(_auth_app(keypair)).get("/billing", headers=_bearer_headers(token))

    assert response.status_code == 200


def test_role_from_another_client_does_not_grant_access(keypair: RsaKeyPair) -> None:
    """A role granted on a *different* client is not a role here.

    resource_access carries every client the user has roles on, so reading the
    wrong entry would let a role from an unrelated service authorise calls to
    this one.
    """
    token = issue_test_token(
        keypair,
        issuer=KC.issuer,
        audience=KC.client_id,
        client_roles={"some-other-api": ["billing"]},
    )
    response = TestClient(_auth_app(keypair)).get("/billing", headers=_bearer_headers(token))

    assert response.status_code == 403


def test_missing_role_is_forbidden_not_unauthorized(keypair: RsaKeyPair) -> None:
    """A valid token without the role is 403: authenticated, not permitted."""
    token = issue_test_token(
        keypair, issuer=KC.issuer, audience=KC.client_id, realm_roles=["viewer"]
    )
    response = TestClient(_auth_app(keypair)).get("/admin", headers=_bearer_headers(token))

    assert response.status_code == 403
    # The rule that failed is named, so a 403 can be debugged from the response
    # instead of by reading the route.
    assert response.json()["detail"] == "Insufficient permissions; requires: role:admin"


def test_any_of_false_requires_every_role(keypair: RsaKeyPair) -> None:
    client = TestClient(_auth_app(keypair))
    one = issue_test_token(keypair, issuer=KC.issuer, audience=KC.client_id, realm_roles=["admin"])
    both = issue_test_token(
        keypair, issuer=KC.issuer, audience=KC.client_id, realm_roles=["admin", "auditor"]
    )

    assert client.get("/both", headers=_bearer_headers(one)).status_code == 403
    assert client.get("/both", headers=_bearer_headers(both)).status_code == 200


# --- azp: which client obtained the token ---------------------------------
#
# `aud` does not answer this. Keycloak fills it from the clients the subject
# holds roles on, so any client in the realm can obtain a token this API
# accepts. `allowed_azp` is the way to narrow that, and it is off unless set.


def _azp_token(keypair: RsaKeyPair, azp: str | None) -> str:
    extra = {"azp": azp} if azp is not None else {}
    return issue_test_token(keypair, issuer=KC.issuer, audience=KC.client_id, extra_claims=extra)


def _validator_allowing(keypair: RsaKeyPair, *azp: str) -> KeycloakTokenValidator:
    validator = _validator_with_key(keypair)
    validator.settings = KC.model_copy(update={"allowed_azp": list(azp)})
    return validator


def test_azp_is_not_checked_until_a_service_asks_for_it(keypair: RsaKeyPair) -> None:
    """Off by default. Upgrading must not start rejecting anyone's traffic."""
    assert _validator_with_key(keypair).decode(_azp_token(keypair, "anything-at-all")).sub


def test_a_token_from_an_allowed_client_passes(keypair: RsaKeyPair) -> None:
    validator = _validator_allowing(keypair, "web-spa", "mobile-app")

    assert validator.decode(_azp_token(keypair, "mobile-app")).sub


def test_a_token_from_another_client_in_the_realm_is_rejected(keypair: RsaKeyPair) -> None:
    """The hole this setting exists to close.

    The token is genuine, correctly signed, and its audience check passes — it
    was simply obtained by a client this service does not trust.
    """
    validator = _validator_allowing(keypair, "web-spa")

    with pytest.raises(HTTPException) as rejected:
        validator.decode(_azp_token(keypair, "partner-integration"))

    assert rejected.value.status_code == 401
    assert rejected.value.detail == "Token was not issued to a client this service accepts"
    # A distinct message on purpose: the audience rejection answers a bare
    # "Invalid or expired token", which cost real time to diagnose once already.
    assert rejected.value.headers == {"WWW-Authenticate": "Bearer"}


def test_a_token_with_no_azp_is_rejected_once_a_list_exists(keypair: RsaKeyPair) -> None:
    """Fail closed. A token that will not say where it came from is not on the list."""
    validator = _validator_allowing(keypair, "web-spa")

    with pytest.raises(HTTPException):
        validator.decode(_azp_token(keypair, None))


# --- service-to-service tokens --------------------------------------------
#
# ClientCredentialsTokenProvider is what every outbound service call goes
# through. None of it was exercised: a bug here does not surface at deploy
# time, it surfaces when the first token expires.

TOKEN_RESPONSE = {"access_token": "tok-1", "expires_in": 300}


def _token_handler(
    calls: list[httpx.Request], *responses: dict[str, Any]
) -> Callable[[httpx.Request], httpx.Response]:
    queue = list(responses) or [TOKEN_RESPONSE]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(200, json=payload)

    return handler


async def test_token_is_fetched_with_client_credentials() -> None:
    provider = ClientCredentialsTokenProvider(settings=KC_CONFIDENTIAL)
    calls: list[httpx.Request] = []

    with _patched_async_client(_token_handler(calls)):
        token = await provider.get_token()

    assert token == "tok-1"
    assert str(calls[0].url) == KC_CONFIDENTIAL.token_url
    assert dict(parse_qsl(calls[0].content.decode())) == {
        "grant_type": "client_credentials",
        "client_id": KC_CONFIDENTIAL.client_id,
        "client_secret": KC_CONFIDENTIAL.client_secret,
    }


async def test_no_scope_is_sent_when_none_is_configured() -> None:
    """The default request stays byte-identical to what it always was.

    A service that never configures a scope must see no change on the wire --
    and an empty ``scope=`` is not the same as no ``scope`` at all.
    """
    provider = ClientCredentialsTokenProvider(settings=KC_CONFIDENTIAL)
    calls: list[httpx.Request] = []

    with _patched_async_client(_token_handler(calls)):
        await provider.get_token()

    # keep_blank_values, or this assertion cannot see the difference it exists
    # for: parse_qsl drops `scope=` silently, so sending an empty scope instead
    # of omitting the key would read as identical. (It did, until a mutation
    # check caught it.)
    sent = dict(parse_qsl(calls[0].content.decode(), keep_blank_values=True))
    assert "scope" not in sent


async def test_the_configured_scope_is_requested() -> None:
    """Without this the grant can only ever carry the client's default scopes,
    so no ``HasScope`` rule naming a business scope could pass for an S2S
    caller. Verified against a real Keycloak in
    ``tests/integration/test_keycloak_integration.py``."""
    settings = KC_CONFIDENTIAL.model_copy(update={"token_scope": "orders:write orders:read"})
    provider = ClientCredentialsTokenProvider(settings=settings)
    calls: list[httpx.Request] = []

    with _patched_async_client(_token_handler(calls)):
        await provider.get_token()

    assert dict(parse_qsl(calls[0].content.decode()))["scope"] == "orders:write orders:read"


async def test_token_is_cached_until_it_nears_expiry() -> None:
    provider = ClientCredentialsTokenProvider(settings=KC_CONFIDENTIAL)
    calls: list[httpx.Request] = []

    with _patched_async_client(_token_handler(calls)):
        first = await provider.get_token()
        second = await provider.get_token()

    assert first == second
    assert len(calls) == 1


async def test_token_is_refetched_once_it_has_expired() -> None:
    provider = ClientCredentialsTokenProvider(settings=KC_CONFIDENTIAL)
    calls: list[httpx.Request] = []
    handler = _token_handler(
        calls,
        {"access_token": "tok-1", "expires_in": 300},
        {"access_token": "tok-2", "expires_in": 300},
    )

    with _patched_async_client(handler):
        assert await provider.get_token() == "tok-1"
        provider._expires_at = time.monotonic() - 1  # as if the clock moved past it
        assert await provider.get_token() == "tok-2"

    assert len(calls) == 2


async def test_expiry_is_shortened_by_the_refresh_leeway() -> None:
    """The cached token is dropped *before* Keycloak would reject it.

    Renewing exactly at expiry leaves no room for the clock skew and flight time
    between issuing the token and the callee checking it, so a token that is
    technically still valid can arrive expired.
    """
    provider = ClientCredentialsTokenProvider(settings=KC_CONFIDENTIAL, refresh_leeway_seconds=30)
    calls: list[httpx.Request] = []

    with _patched_async_client(_token_handler(calls, {"access_token": "t", "expires_in": 300})):
        before = time.monotonic()
        await provider.get_token()

    assert provider._expires_at - before == pytest.approx(300 - 30, abs=1)


async def test_a_lifetime_below_the_leeway_still_caches_briefly() -> None:
    """expires_in under the leeway must not produce an already-expired cache.

    ``max(..., 1.0)`` is what stops every single call from re-fetching when a
    short-lived token is issued.
    """
    provider = ClientCredentialsTokenProvider(settings=KC_CONFIDENTIAL, refresh_leeway_seconds=30)
    calls: list[httpx.Request] = []

    with _patched_async_client(_token_handler(calls, {"access_token": "t", "expires_in": 5})):
        before = time.monotonic()
        await provider.get_token()
        await provider.get_token()

    assert provider._expires_at - before == pytest.approx(1.0, abs=0.5)
    assert len(calls) == 1


async def test_missing_expires_in_falls_back_to_a_minute() -> None:
    provider = ClientCredentialsTokenProvider(settings=KC_CONFIDENTIAL, refresh_leeway_seconds=0)
    calls: list[httpx.Request] = []

    with _patched_async_client(_token_handler(calls, {"access_token": "t"})):
        before = time.monotonic()
        await provider.get_token()

    assert provider._expires_at - before == pytest.approx(60, abs=1)


async def test_invalidate_forces_the_next_call_to_refetch() -> None:
    provider = ClientCredentialsTokenProvider(settings=KC_CONFIDENTIAL)
    calls: list[httpx.Request] = []
    handler = _token_handler(
        calls,
        {"access_token": "tok-1", "expires_in": 300},
        {"access_token": "tok-2", "expires_in": 300},
    )

    with _patched_async_client(handler):
        assert await provider.get_token() == "tok-1"
        provider.invalidate()
        assert await provider.get_token() == "tok-2"

    assert len(calls) == 2


async def test_concurrent_callers_fetch_the_token_once() -> None:
    """The lock exists so a cold start does not stampede Keycloak.

    Without it, every request that arrives before the first token lands issues
    its own token request -- exactly when the service is least able to afford
    the extra load.
    """
    provider = ClientCredentialsTokenProvider(settings=KC_CONFIDENTIAL)
    calls: list[httpx.Request] = []

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        # Async, and it actually suspends: a handler that answers without
        # yielding lets the first caller finish the whole critical section
        # before the others are scheduled, and then the test passes whether or
        # not the lock is there.
        calls.append(request)
        await anyio.sleep(0.01)
        return httpx.Response(200, json=TOKEN_RESPONSE)

    results: list[str] = []

    async def fetch() -> None:
        results.append(await provider.get_token())

    with _patched_async_client(slow_handler):
        async with anyio.create_task_group() as tg:
            for _ in range(10):
                tg.start_soon(fetch)

    assert results == ["tok-1"] * 10
    assert len(calls) == 1


async def test_a_rejected_client_credential_raises() -> None:
    """An invalid secret must not be cached as if it were a token."""
    provider = ClientCredentialsTokenProvider(settings=KC_CONFIDENTIAL)

    with (
        _patched_async_client(lambda _: httpx.Response(401, json={"error": "invalid_client"})),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await provider.get_token()

    assert provider._token is None


def test_refresh_that_still_fails_is_rejected_once(keypair: RsaKeyPair) -> None:
    """A token that stays invalid after the JWKS refresh is 401, not a retry loop.

    The refresh exists for key rotation. A forged token also lands here, and it
    must cost Keycloak exactly one extra fetch -- never a request per attempt.
    """
    from jwt.exceptions import PyJWKClientError

    token = issue_test_token(keypair, issuer=KC.issuer, audience=KC.client_id)
    other = generate_rsa_keypair()
    validator = KeycloakTokenValidator(settings=KC)

    stale = MagicMock()
    stale.get_signing_key_from_jwt.side_effect = PyJWKClientError("unknown kid")
    wrong_key = MagicMock()
    wrong_key.key = other.public_pem
    refreshed = MagicMock()
    refreshed.get_signing_key_from_jwt.return_value = wrong_key

    calls: list[bool] = []

    def fake_get(*, force_refresh: bool = False) -> Any:
        calls.append(force_refresh)
        return refreshed if force_refresh else stale

    with (
        patch.object(validator, "_get_jwks_client", side_effect=fake_get),
        pytest.raises(HTTPException) as raised,
    ):
        validator.decode(token)

    assert raised.value.status_code == 401
    assert calls == [False, True]
