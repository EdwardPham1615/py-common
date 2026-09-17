"""Where authentication and authorization attach to an application.

The split this module exists to make:

**Authentication is a property of a family of routes.** Every endpoint under
``/api/v1`` wants a valid JWT; there is no interesting per-route decision, and
the only realistic mistake is forgetting one — which yields a silently public
endpoint. So it goes on the router, where routes inherit it structurally and
adding an endpoint cannot miss it.

**Authorization is a decision per endpoint.** There is no correct default for
"who may delete this", so it must be written at the route and read in review.
Wrapping it into a router would both breed a router per combination of rights
and turn a deliberate decision into something inherited without being reread.

It is worth saying why none of this is middleware, because middleware is the
obvious first idea and it is wrong in four separate ways. Starlette middleware
runs *before* routing, so it can only match raw paths — and a path-pattern
exemption list is the classic bypass surface (trailing slashes, ``//``, encoded
dots, a new route that happens to share an exempt prefix). An ``HTTPException``
raised in middleware never reaches FastAPI's handlers, which live inside the
router, so its 401 would not be ``problem+json``; raised from a dependency, as
here, it is. Middleware is invisible to OpenAPI, so Swagger's Authorize button
and the documented 401 both disappear. And middleware cannot inject
:class:`~py_common.security.keycloak.TokenClaims` into a handler signature —
claims would travel through ``request.state``, untyped.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from py_common.logging import get_logger
from py_common.security.keycloak import TokenClaims, unauthorized
from py_common.security.requirements import AllOf, HasRole

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from py_common.config import BaseAppSettings
    from py_common.security.keycloak import KeycloakTokenValidator
    from py_common.security.requirements import Requirement

__all__ = [
    "INTERNAL_API_KEY_HEADER",
    "Auth",
    "internal_router",
    "protected_router",
]

INTERNAL_API_KEY_HEADER = "X-API-Key"

logger = get_logger(__name__)

_bearer = HTTPBearer(auto_error=False)
_api_key = APIKeyHeader(name=INTERNAL_API_KEY_HEADER, auto_error=False)


@dataclass
class Auth:
    """Binds a token validator to the FastAPI dependencies that use it.

    Two layers, usable independently::

        auth = Auth(KeycloakTokenValidator(settings.keycloak))

        api = protected_router(auth, prefix="/api/v1")   # authentication

        @api.post("/orders", dependencies=[Depends(auth.requires(
            HasRole("admin") | HasScope("orders:write")))])   # authorization
        async def create_order() -> None: ...
    """

    validator: KeycloakTokenValidator
    _current_user: Callable[..., Awaitable[TokenClaims]] = field(init=False, repr=False)
    _optional_user: Callable[..., Awaitable[TokenClaims | None]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Build each dependency exactly once and hold on to it. FastAPI caches a
        # resolved dependency per request keyed on the callable itself, so a
        # property that returned a fresh closure on every access would defeat
        # that: a request whose router authenticates it and whose route also
        # asks for the claims would validate the same token twice, and pay for
        # two JWKS lookups on a cold cache.
        validator = self.validator

        async def current_user(
            credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
        ) -> TokenClaims:
            if credentials is None or credentials.scheme.lower() != "bearer":
                raise unauthorized("Not authenticated")
            return await validator.decode_async(credentials.credentials)

        async def optional_user(
            credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
        ) -> TokenClaims | None:
            if credentials is None or credentials.scheme.lower() != "bearer":
                return None
            return await validator.decode_async(credentials.credentials)

        self._current_user = current_user
        self._optional_user = optional_user

    @property
    def current_user(self) -> Callable[..., Awaitable[TokenClaims]]:
        """Authentication. 401 unless the request carries a valid bearer token."""
        return self._current_user

    @property
    def optional_user(self) -> Callable[..., Awaitable[TokenClaims | None]]:
        """Authentication that does not insist: ``None`` when no token is present.

        For an endpoint that serves everyone but serves signed-in callers
        differently. An *invalid* token still raises — silently treating a
        malformed or expired token as "anonymous" would hide a broken client.
        """
        return self._optional_user

    def requires(self, requirement: Requirement) -> Callable[..., Awaitable[TokenClaims]]:
        """Authorization. 403 unless ``requirement`` holds for the caller's claims."""

        async def checker(user: TokenClaims = Depends(self._current_user)) -> TokenClaims:
            if not requirement.check(user):
                # Name the rule that failed. This does tell an authenticated
                # caller what the policy is -- a deliberate trade: on an
                # internal platform, a 403 you cannot debug costs far more than
                # the rule being known to someone who already holds a token.
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Insufficient permissions; requires: {requirement.describe()}",
                )
            return user

        return checker

    def require_roles(
        self, *roles: str, any_of: bool = True
    ) -> Callable[..., Awaitable[TokenClaims]]:
        """Shorthand for the common case, so it needs no knowledge of the algebra.

        ``any_of=True`` passes on any one of ``roles``; ``False`` demands all of
        them. Equivalent to ``requires(HasRole(*roles))`` and
        ``requires(AllOf(*(HasRole(r) for r in roles)))`` respectively.
        """
        if not roles:
            raise ValueError("require_roles needs at least one role")
        requirement = HasRole(*roles) if any_of else AllOf(*(HasRole(role) for role in roles))
        return self.requires(requirement)


def protected_router(auth: Auth, **kwargs: Any) -> APIRouter:
    """An ``APIRouter`` whose every route requires a valid JWT.

    The dependency is inherited by routes added directly and by routers included
    into this one, so a nested ``include_router`` cannot open a hole. It also
    reaches OpenAPI: each operation gains ``security`` and the document gains
    the bearer scheme, which is what makes Swagger's Authorize button work.
    """
    dependencies = [Depends(auth.current_user), *kwargs.pop("dependencies", [])]
    return APIRouter(dependencies=dependencies, **kwargs)


def internal_router(settings: BaseAppSettings, **kwargs: Any) -> APIRouter:
    """An ``APIRouter`` for internal endpoints, guarded by ``X-API-Key``.

    The guard is installed only when ``HTTP__INTERNAL_API_KEY`` is set. With no
    key configured the router is an ordinary one and the routes are open —
    reasonable when the network already keeps them unreachable, but a decision
    rather than an accident, so it is logged at construction and
    :func:`~py_common.testing.routes.assert_routes_protected` reports those
    routes as unprotected until they are written into its allowlist.
    """
    expected = settings.http.internal_api_key
    if not expected:
        logger.warning(
            "internal_router_unauthenticated",
            reason="HTTP__INTERNAL_API_KEY is not set; internal routes rely on network isolation",
        )
        return APIRouter(**kwargs)

    async def check_api_key(provided: str | None = Depends(_api_key)) -> None:
        # compare_digest, not ==, so the comparison does not leak the key's
        # length or prefix through timing to a caller who can guess repeatedly.
        if provided is None or not secrets.compare_digest(provided, expected):
            # No WWW-Authenticate header: an API key in a bespoke header is not
            # an HTTP authentication scheme, and answering "Bearer" here would
            # point the caller at a challenge this route does not accept.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
            )

    dependencies = [Depends(check_api_key), *kwargs.pop("dependencies", [])]
    return APIRouter(dependencies=dependencies, **kwargs)
