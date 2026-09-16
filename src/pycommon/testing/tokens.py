"""JWT test-token factory — write auth tests without a running Keycloak.

Requires the ``security`` extra (PyJWT + cryptography).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@dataclass(frozen=True)
class RsaKeyPair:
    private_pem: str
    public_pem: str
    kid: str


def generate_rsa_keypair(*, kid: str | None = None) -> RsaKeyPair:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return RsaKeyPair(private_pem=private_pem, public_pem=public_pem, kid=kid or uuid.uuid4().hex)


def issue_test_token(
    keypair: RsaKeyPair,
    *,
    issuer: str,
    audience: str,
    sub: str = "test-user",
    realm_roles: list[str] | None = None,
    client_roles: dict[str, list[str]] | None = None,
    scopes: list[str] | None = None,
    expires_in_seconds: int = 300,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Issue a Keycloak-shaped RS256 token signed with the test keypair."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + expires_in_seconds,
        "realm_access": {"roles": realm_roles or []},
        "resource_access": {
            client: {"roles": roles} for client, roles in (client_roles or {}).items()
        },
        # Space-separated, as RFC 6749 defines it -- a list here would parse to
        # no scopes at all and make a test pass for the wrong reason.
        **({"scope": " ".join(scopes)} if scopes else {}),
        **(extra_claims or {}),
    }
    return jwt.encode(
        payload,
        keypair.private_pem,
        algorithm="RS256",
        headers={"kid": keypair.kid},
    )
