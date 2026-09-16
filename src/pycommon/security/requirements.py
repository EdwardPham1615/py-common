"""Authorization requirements: small composable predicates over ``TokenClaims``.

Authorization rules are rarely one dimension. "Role ``admin`` **or** scope
``orders:write``" cannot be written with a ``require_roles`` and a
``require_scopes`` sitting side by side, and adding a third function for every
new axis never fixes that — each one can only say something about its own axis.

So the primitives compose instead. Each takes one or more values meaning *any of
these*, and ``|`` / ``&`` join different kinds together::

    HasRole("admin", "auditor")                      # either role
    HasRole("admin") | HasScope("orders:write")      # either dimension
    HasRole("support") | (HasRole("agent") & HasScope("tickets:close"))

Pass the result to :meth:`~pycommon.security.auth.Auth.requires`.

There is deliberately no negation. A rule phrased as "anyone except …" is a
denial list, and denial lists fail open the moment someone adds a role nobody
thought about. :class:`Custom` is there for the cases that genuinely need
something else.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pycommon.security.keycloak import TokenClaims

__all__ = [
    "AllOf",
    "AnyOf",
    "Custom",
    "HasRole",
    "HasScope",
    "Requirement",
]


class Requirement:
    """Base class for an authorization rule.

    Subclasses implement :meth:`check` and :meth:`describe`. The operators are
    here rather than on a Protocol because they are the point: a requirement
    that could not be combined would leave us back at one function per axis.
    """

    def check(self, claims: TokenClaims) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def __or__(self, other: Requirement) -> Requirement:
        return AnyOf(*_flatten(AnyOf, self, other))

    def __and__(self, other: Requirement) -> Requirement:
        return AllOf(*_flatten(AllOf, self, other))


def _flatten(kind: type[Requirement], *parts: Requirement) -> tuple[Requirement, ...]:
    """Splice same-kind children in, so ``a | b | c`` is one AnyOf, not two.

    Only affects :meth:`Requirement.describe` — the nested form evaluates the
    same — but a 403 reading ``role:a OR (role:b OR role:c)`` helps nobody.
    """
    out: list[Requirement] = []
    for part in parts:
        if type(part) is kind:
            out.extend(part.parts)  # type: ignore[attr-defined]
        else:
            out.append(part)
    return tuple(out)


@dataclass(frozen=True)
class HasRole(Requirement):
    """Passes when the caller holds *any* of these roles (realm or client).

    Fine-grained rights are usually modelled as client roles in Keycloak, so
    ``HasRole("orders:write")`` is an ordinary thing to write, not a misuse.
    """

    values: tuple[str, ...]

    def __init__(self, *values: str) -> None:
        if not values:
            raise ValueError("HasRole needs at least one role")
        object.__setattr__(self, "values", values)

    def check(self, claims: TokenClaims) -> bool:
        return bool(claims.roles & set(self.values))

    def describe(self) -> str:
        return " OR ".join(f"role:{v}" for v in self.values)


@dataclass(frozen=True)
class HasScope(Requirement):
    """Passes when the token carries *any* of these OAuth2 scopes.

    Scope bounds what the *client application* may do on the caller's behalf, so
    it is the right axis for narrowing a service-to-service caller — not a
    replacement for a role check on a user.
    """

    values: tuple[str, ...]

    def __init__(self, *values: str) -> None:
        if not values:
            raise ValueError("HasScope needs at least one scope")
        object.__setattr__(self, "values", values)

    def check(self, claims: TokenClaims) -> bool:
        return bool(set(claims.scopes) & set(self.values))

    def describe(self) -> str:
        return " OR ".join(f"scope:{v}" for v in self.values)


@dataclass(frozen=True)
class Custom(Requirement):
    """An arbitrary predicate over the claims — including ``claims.raw``.

    The escape hatch for anything the two primitives do not model: a deployment
    that maps permissions into a custom claim, a tenant check, an ownership
    rule. ``description`` is what the 403 will say, so write it for whoever
    reads that response at 3am.
    """

    predicate: Callable[[TokenClaims], bool]
    description: str

    def check(self, claims: TokenClaims) -> bool:
        return bool(self.predicate(claims))

    def describe(self) -> str:
        return self.description


@dataclass(frozen=True)
class AnyOf(Requirement):
    """Passes when at least one part passes. Built by ``|``."""

    parts: tuple[Requirement, ...]

    def __init__(self, *parts: Requirement) -> None:
        if not parts:
            raise ValueError("AnyOf needs at least one requirement")
        object.__setattr__(self, "parts", parts)

    def check(self, claims: TokenClaims) -> bool:
        return any(part.check(claims) for part in self.parts)

    def describe(self) -> str:
        return " OR ".join(_wrap(part) for part in self.parts)


@dataclass(frozen=True)
class AllOf(Requirement):
    """Passes only when every part passes. Built by ``&``."""

    parts: tuple[Requirement, ...]

    def __init__(self, *parts: Requirement) -> None:
        if not parts:
            raise ValueError("AllOf needs at least one requirement")
        object.__setattr__(self, "parts", parts)

    def check(self, claims: TokenClaims) -> bool:
        return all(part.check(claims) for part in self.parts)

    def describe(self) -> str:
        return " AND ".join(_wrap(part) for part in self.parts)


def _wrap(part: Requirement) -> str:
    """Parenthesise anything that already reads as a combination.

    Keyed on the rendered text rather than the type, because a multi-value
    primitive prints as a combination too: ``HasRole("a", "b")`` inside an AllOf
    has to come out ``(role:a OR role:b) AND …`` or the 403 states a different
    rule than the one that was enforced.
    """
    text = part.describe()
    return f"({text})" if " OR " in text or " AND " in text else text
