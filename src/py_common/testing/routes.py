"""Prove, in a service's own test suite, that no route was left unauthenticated.

The failure this exists for is quiet: someone adds an endpoint outside the
authenticated router, nothing raises, no test fails, and the endpoint is public
until somebody notices. :func:`assert_routes_protected` turns that into a red
test naming the route.

Requires the ``http`` extra.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from fastapi import FastAPI

__all__ = ["PYCOMMON_PUBLIC_PREFIXES", "assert_routes_protected"]

#: The routers py_common itself ships that are normally reachable unauthenticated:
#: probes, the problem-type documentation, the metrics scrape endpoint. Splat it
#: into ``public_prefixes`` if that is what you intend. It is a constant rather
#: than a default so that every open endpoint appears at a call site.
PYCOMMON_PUBLIC_PREFIXES = ("/health", "/problems", "/metrics")


#: Must equal :data:`py_common.security.OPTIONAL_AUTH_SCHEME`. Spelled out rather
#: than imported: this module needs only the ``http`` extra, while
#: ``py_common.security`` pulls PyJWT, and importing it here would make
#: ``assert_routes_protected`` unusable for a consumer who installed
#: ``py-common[http]`` alone -- the exact failure mode `make extras-check` exists
#: to prevent. `test_routes.py` asserts the two stay equal, so they cannot drift.
_OPTIONAL_AUTH_SCHEME = "OptionalBearer"


def _is_protected(security: Any) -> bool:
    """True when at least one declared requirement actually demands credentials.

    ``Auth.optional_user`` reads the same ``Authorization`` header as
    ``current_user`` but lets an anonymous caller through, so a route using it is
    open no matter what the header says. It declares
    ``py_common.security.OPTIONAL_AUTH_SCHEME`` for exactly this reason —
    without a separate scheme name the generated document is identical to a
    protected route's, and this check counted such routes as protected.

    An empty requirement object (``{}``) means "no credentials needed" in OpenAPI
    and is treated the same way.
    """
    if not security:
        return False
    for requirement in security:
        if not isinstance(requirement, dict) or not requirement:
            continue
        if set(requirement) == {_OPTIONAL_AUTH_SCHEME}:
            continue
        return True
    return False


def assert_routes_protected(
    app: FastAPI,
    *,
    public_prefixes: Iterable[str] = (),
    public_paths: Iterable[str] = (),
) -> None:
    """Fail unless every operation either declares a security scheme or is listed.

    Reads ``app.openapi()`` rather than walking ``app.routes``: FastAPI keeps
    included routers behind a private, lazily-resolved object with no public way
    to enumerate their effective dependencies, while the OpenAPI document is
    public API and already reflects dependencies inherited from a router — the
    bearer dependency on a protected router, the ``X-API-Key`` one on an
    internal router.

    Two things it does *not* check, worth knowing before trusting it:

    * **Authorization.** A route that authenticates but forgets its
      ``requires(...)`` passes here. That is by design — there is no default
      authorization rule to compare against — so this covers the mistake that is
      silent, not the one that is a decision.
    * **Protection that declares no security scheme.** The check reads the
      generated document, so a route guarded by a plain dependency (not built on
      ``HTTPBearer``/``APIKeyHeader``) reads as unprotected and has to be listed.

    Optional authentication *is* handled: a route depending on
    ``Auth.optional_user`` declares
    ``py_common.security.OPTIONAL_AUTH_SCHEME`` instead of the required-auth
    scheme, and counts as **open** — anyone may call it — so it has to appear in
    the allowlist like any other public route. Before that scheme existed, such a
    route was indistinguishable from a protected one here and passed silently.

    The result also reflects the settings *this* app was built with. An
    ``internal_router`` created without ``HTTP__INTERNAL_API_KEY`` installs no
    guard, so it will be reported here even if production sets the key — build
    the app under production-shaped settings if you want this to mean something
    about production.

    :raises AssertionError: listing every unprotected ``METHOD /path``.
    """
    prefixes = tuple(public_prefixes)
    exact = set(public_paths)

    unprotected: list[str] = []
    paths: dict[str, dict[str, Any]] = app.openapi().get("paths", {})
    for path, operations in paths.items():
        if path in exact or path.startswith(prefixes):
            continue
        for method, operation in operations.items():
            # Siblings of the operations: "parameters", "servers", "summary".
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            if not _is_protected(operation.get("security")):
                unprotected.append(f"{method.upper()} {path}")

    if unprotected:
        listing = "\n  ".join(sorted(unprotected))
        raise AssertionError(
            "These operations declare no security scheme. Move them onto an "
            "authenticated router, or list them as public if that is intended:"
            f"\n  {listing}"
        )
