"""Service-to-service token provider (OAuth2 client_credentials against Keycloak)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import anyio
import httpx

from py_common.config import KeycloakSettings


@dataclass
class ClientCredentialsTokenProvider:
    """Fetches and caches a service-account access token for outbound calls.

    Requests ``KEYCLOAK__TOKEN_SCOPE`` when one is set. Without it the token
    carries only the client's default scopes, so a ``HasScope`` rule naming a
    business scope can never pass for a service-to-service caller — the reason
    that setting exists.

    Usage::

        provider = ClientCredentialsTokenProvider(settings.keycloak)
        headers = {"Authorization": f"Bearer {await provider.get_token()}"}
    """

    settings: KeycloakSettings
    refresh_leeway_seconds: float = 30.0
    _token: str | None = field(default=None, init=False, repr=False)
    _expires_at: float = field(default=0.0, init=False, repr=False)
    _lock: anyio.Lock = field(default_factory=anyio.Lock, init=False, repr=False)

    async def get_token(self) -> str:
        async with self._lock:
            if self._token is not None and time.monotonic() < self._expires_at:
                return self._token

            form = {
                "grant_type": "client_credentials",
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
            }
            # Omitted entirely when unset, rather than sent empty: the request
            # then stays byte-identical to what it has always been, and a
            # deployment that never configures a scope sees no change.
            if self.settings.token_scope:
                form["scope"] = self.settings.token_scope

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self.settings.token_url,
                    data=form,
                    timeout=10.0,
                )
                resp.raise_for_status()
                payload = resp.json()

            self._token = str(payload["access_token"])
            expires_in = float(payload.get("expires_in", 60))
            self._expires_at = time.monotonic() + max(expires_in - self.refresh_leeway_seconds, 1.0)
            return self._token

    def invalidate(self) -> None:
        """Drop the cached token (e.g. after receiving a 401 downstream)."""
        self._token = None
        self._expires_at = 0.0
