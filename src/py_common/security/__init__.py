"""Keycloak OIDC JWT validation, route-level auth policy, and service-to-service auth."""

from py_common.security.auth import (
    INTERNAL_API_KEY_HEADER,
    Auth,
    internal_router,
    protected_router,
)
from py_common.security.keycloak import (
    KeycloakTokenValidator,
    TokenClaims,
    unauthorized,
)
from py_common.security.requirements import (
    AllOf,
    AnyOf,
    Custom,
    HasRole,
    HasScope,
    Requirement,
)
from py_common.security.service_token import ClientCredentialsTokenProvider

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
