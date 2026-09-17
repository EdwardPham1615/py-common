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

import jwt
import pytest
from fastapi import HTTPException
from jwt import PyJWKClient

from pycommon.security import ClientCredentialsTokenProvider, KeycloakTokenValidator

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
    assert claims.scopes == ["profile", "email"]


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
    nothing in pycommon checks it today.
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
