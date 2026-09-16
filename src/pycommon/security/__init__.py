"""Keycloak OIDC JWT validation, route-level auth policy, and service-to-service auth."""

from pycommon.security.auth import (
    INTERNAL_API_KEY_HEADER,
    Auth,
    internal_router,
    protected_router,
)
from pycommon.security.keycloak import (
    KeycloakTokenValidator,
    TokenClaims,
    unauthorized,
)
from pycommon.security.requirements import (
    AllOf,
    AnyOf,
    Custom,
    HasRole,
    HasScope,
    Requirement,
)
from pycommon.security.service_token import ClientCredentialsTokenProvider

__all__ = [
    "INTERNAL_API_KEY_HEADER",
    "AllOf",
    "AnyOf",
    "Auth",
    "ClientCredentialsTokenProvider",
    "Custom",
    "HasRole",
    "HasScope",
    "KeycloakTokenValidator",
    "Requirement",
    "TokenClaims",
    "internal_router",
    "protected_router",
    "unauthorized",
]
