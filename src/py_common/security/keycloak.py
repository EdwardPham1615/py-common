"""Keycloak OIDC JWT validation: the token validator and the claims it yields."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import anyio.to_thread
import httpx
import jwt
from fastapi import HTTPException, status
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientError
from pydantic import BaseModel, Field

from py_common.config import KeycloakSettings


class TokenClaims(BaseModel):
    """The parts of a validated access token this library reasons about.

    Two axes, and they answer different questions. ``roles`` is *who* the caller
    is allowed to be — it belongs to the user (or to a service account, which
    Keycloak gives client roles exactly like a person). ``scopes`` is *what the
    client application* may ask for on the caller's behalf. A delegated token is
    correctly checked against both; neither substitutes for the other.

    Anything else a deployment puts in its tokens is reachable through
    :attr:`raw`, which is what :class:`~py_common.security.requirements.Custom`
    exists to read.
    """

    sub: str
    email: str | None = None
    preferred_username: str | None = None
    name: str | None = None
    realm_roles: list[str] = Field(default_factory=list)
    client_roles: list[str] = Field(default_factory=list)
    # From the standard OAuth2 ``scope`` claim, split on whitespace. Needs no
    # configuration: every Keycloak access token carries it.
    scopes: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def roles(self) -> set[str]:
        """Realm and client roles as one set.

        Naming the union rather than introducing it: authorization has always
        been decided against both lists together, so the realm/client split has
        never reached a decision. Code that needs the distinction still has both
        fields.
        """
        return set(self.realm_roles) | set(self.client_roles)


def unauthorized(detail: str = "Invalid or expired token") -> HTTPException:
    """A 401 carrying the bearer challenge, so every rejection looks the same."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


@dataclass
class KeycloakTokenValidator:
    settings: KeycloakSettings
    _jwks_client: PyJWKClient | None = field(default=None, init=False, repr=False)
    _jwks_fetched_at: float = field(default=0.0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _get_jwks_client(self, *, force_refresh: bool = False) -> PyJWKClient:
        now = time.monotonic()
        ttl = self.settings.jwks_cache_ttl_seconds
        with self._lock:
            expired = self._jwks_client is None or (now - self._jwks_fetched_at) > ttl
            if force_refresh or expired:
                self._jwks_client = PyJWKClient(
                    self.settings.jwks_url,
                    cache_keys=True,
                    lifespan=ttl,
                )
                self._jwks_fetched_at = now
            assert self._jwks_client is not None
            return self._jwks_client

    def _decode_once(self, token: str, *, force_refresh: bool = False) -> dict[str, Any]:
        jwks = self._get_jwks_client(force_refresh=force_refresh)
        signing_key = jwks.get_signing_key_from_jwt(token)
        audience = self.settings.audience or self.settings.client_id
        result: dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            algorithms=self.settings.algorithms,
            audience=audience if self.settings.verify_aud else None,
            issuer=self.settings.issuer,
            options={"verify_aud": self.settings.verify_aud},
        )
        return result

    def decode(self, token: str) -> TokenClaims:
        """Validate a bearer token and map its payload to :class:`TokenClaims`.

        Note: JWKS fetches use blocking I/O — from async code call
        :meth:`decode_async` instead.
        """
        try:
            payload = self._decode_once(token)
        except PyJWKClientError:
            # Unknown kid — likely key rotation. Refresh JWKS exactly once.
            # Other JWT errors (expired, bad audience/signature) must NOT
            # trigger a refetch, or garbage tokens would hammer Keycloak.
            try:
                payload = self._decode_once(token, force_refresh=True)
            except jwt.PyJWTError as retry_exc:
                raise unauthorized() from retry_exc
        except jwt.PyJWTError as exc:
            raise unauthorized() from exc

        # Checked here rather than inside _decode_once so it can never be
        # mistaken for a signing failure and send us back to Keycloak for a
        # fresh JWKS: the signature was fine, the issuing client was not.
        #
        # A missing azp is a rejection, not a pass. Once a service has declared
        # which clients it trusts, a token that will not say where it came from
        # cannot be one of them.
        if self.settings.allowed_azp and payload.get("azp") not in self.settings.allowed_azp:
            raise unauthorized("Token was not issued to a client this service accepts")

        realm_access = payload.get("realm_access") or {}
        resource_access = payload.get("resource_access") or {}
        client_roles = (resource_access.get(self.settings.client_id) or {}).get("roles") or []

        # The scope claim is a single space-separated string, not a list --
        # RFC 6749 section 3.3. A token without one yields no scopes rather
        # than failing: plenty of valid tokens carry none.
        scope_claim = payload.get("scope")
        scopes = scope_claim.split() if isinstance(scope_claim, str) else []

        return TokenClaims(
            sub=payload["sub"],
            email=payload.get("email"),
            preferred_username=payload.get("preferred_username"),
            name=payload.get("name"),
            realm_roles=list(realm_access.get("roles") or []),
            client_roles=list(client_roles),
            scopes=scopes,
            raw=payload,
        )

    async def decode_async(self, token: str) -> TokenClaims:
        """Like :meth:`decode` but in a worker thread so JWKS fetches never block the event loop."""
        return await anyio.to_thread.run_sync(self.decode, token)

    async def fetch_openid_config(self) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(self.settings.openid_config_url, timeout=10.0)
            resp.raise_for_status()
            result: dict[str, Any] = resp.json()
            return result
