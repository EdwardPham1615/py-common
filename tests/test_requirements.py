"""The authorization algebra: what each primitive means, and how they combine.

The reason this file exists as its own area: ``require_roles`` could never
express "role A *or* scope B", and adding one function per axis would never have
fixed it. These tests are the evidence that the composition actually works
end-to-end -- through a real request, not just by calling ``check()``, because a
predicate that is correct in isolation and unreachable through FastAPI would
still be a broken feature.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from pycommon.security import Auth, Custom, HasRole, HasScope, Requirement, TokenClaims
from pycommon.security.requirements import AllOf, AnyOf


class _StubValidator:
    """Stands in for KeycloakTokenValidator: token text is irrelevant here.

    These tests are about the predicates, not about JWT verification, which
    tests/test_security.py already covers against a real signed token.
    """

    def __init__(self, claims: TokenClaims) -> None:
        self.claims = claims
        self.calls = 0

    async def decode_async(self, token: str) -> TokenClaims:
        self.calls += 1
        return self.claims


def _claims(*, roles: list[str] | None = None, scopes: list[str] | None = None) -> TokenClaims:
    return TokenClaims(sub="u-1", realm_roles=roles or [], scopes=scopes or [])


def _client(requirement: Requirement, claims: TokenClaims) -> TestClient:
    auth = Auth(_StubValidator(claims))  # type: ignore[arg-type]
    app = FastAPI()

    @app.get("/guarded", dependencies=[Depends(auth.requires(requirement))])
    async def guarded() -> dict[str, str]:
        return {"ok": "yes"}

    return TestClient(app)


def _status(requirement: Requirement, claims: TokenClaims) -> int:
    response = _client(requirement, claims).get("/guarded", headers={"Authorization": "Bearer tok"})
    return response.status_code


# --- the claim sources ----------------------------------------------------
#
# Where scopes come from is asserted against a real signed token in
# tests/test_security.py -- building TokenClaims by hand here would only
# test the constructor.


def test_roles_unions_realm_and_client() -> None:
    claims = TokenClaims(sub="u", realm_roles=["a"], client_roles=["b"])

    assert claims.roles == {"a", "b"}


def test_absent_claims_yield_empty_rather_than_failing() -> None:
    claims = TokenClaims(sub="u")

    assert claims.scopes == []
    assert claims.roles == set()


# --- the primitives -------------------------------------------------------


def test_has_role_passes_on_any_listed_role() -> None:
    requirement = HasRole("admin", "auditor")

    assert _status(requirement, _claims(roles=["auditor"])) == 200
    assert _status(requirement, _claims(roles=["viewer"])) == 403


def test_has_scope_passes_on_any_listed_scope() -> None:
    requirement = HasScope("orders:read", "orders:write")

    assert _status(requirement, _claims(scopes=["orders:write"])) == 200
    assert _status(requirement, _claims(scopes=["invoices:read"])) == 403


def test_role_and_scope_are_not_interchangeable() -> None:
    """A scope is not a role, and holding one must not satisfy the other.

    They answer different questions -- who the caller is, versus what the client
    application may do on their behalf -- so conflating them would silently turn
    a per-user check into a per-application one.
    """
    assert _status(HasRole("orders:write"), _claims(scopes=["orders:write"])) == 403
    assert _status(HasScope("orders:write"), _claims(roles=["orders:write"])) == 403


def test_custom_reads_the_raw_payload() -> None:
    """The escape hatch for claims this library does not model.

    This is what a deployment with a permissions mapper uses, which is why
    pycommon ships no HasPermission of its own.
    """
    requirement = Custom(
        lambda c: "orders:write" in c.raw.get("permissions", []),
        "permission:orders:write",
    )
    allowed = TokenClaims(sub="u", raw={"permissions": ["orders:write"]})

    assert _status(requirement, allowed) == 200
    assert _status(requirement, TokenClaims(sub="u", raw={"permissions": []})) == 403


@pytest.mark.parametrize("factory", [HasRole, HasScope, AnyOf, AllOf])
def test_an_empty_requirement_is_rejected_at_construction(factory: type) -> None:
    """``HasRole()`` would pass nobody, or -- worse for AllOf -- everybody.

    Better to fail at import than to ship a rule that means nothing.
    """
    with pytest.raises(ValueError, match="at least one"):
        factory()


# --- composition ----------------------------------------------------------


def test_or_passes_when_either_side_does() -> None:
    """The case the old API could not express at all."""
    requirement = HasRole("admin") | HasScope("orders:write")

    assert _status(requirement, _claims(roles=["admin"])) == 200
    assert _status(requirement, _claims(scopes=["orders:write"])) == 200
    assert _status(requirement, _claims(roles=["admin"], scopes=["orders:write"])) == 200
    assert _status(requirement, _claims(roles=["viewer"], scopes=["orders:read"])) == 403


def test_and_needs_both_sides() -> None:
    requirement = HasRole("agent") & HasScope("tickets:close")

    assert _status(requirement, _claims(roles=["agent"], scopes=["tickets:close"])) == 200
    assert _status(requirement, _claims(roles=["agent"])) == 403
    assert _status(requirement, _claims(scopes=["tickets:close"])) == 403


def test_nested_composition_keeps_its_precedence() -> None:
    """``A | (B & C)`` -- the full truth table, because grouping is the whole point.

    If ``&`` and ``|`` were mis-associated, the middle two cases would flip and
    the rule would grant access on B alone.
    """
    requirement = HasRole("support") | (HasRole("agent") & HasScope("tickets:close"))

    assert _status(requirement, _claims(roles=["support"])) == 200
    assert _status(requirement, _claims(roles=["agent"], scopes=["tickets:close"])) == 200
    assert _status(requirement, _claims(roles=["agent"])) == 403
    assert _status(requirement, _claims(scopes=["tickets:close"])) == 403
    assert _status(requirement, _claims()) == 403


def test_chained_operators_flatten_for_readability() -> None:
    """``a | b | c`` reads as one choice of three, not a nest of pairs."""
    assert (HasRole("a") | HasRole("b") | HasRole("c")).describe() == "role:a OR role:b OR role:c"
    assert (HasRole("a") & HasRole("b") & HasRole("c")).describe() == (
        "role:a AND role:b AND role:c"
    )


def test_describe_parenthesises_anything_that_reads_as_a_combination() -> None:
    """Including a multi-value primitive, which prints with its own OR.

    Without the brackets the 403 would state a different rule than the one that
    was enforced -- ``role:a OR role:b AND scope:s`` reads the wrong way round.
    """
    assert (HasRole("a", "b") & HasScope("s")).describe() == "(role:a OR role:b) AND scope:s"
    assert (HasRole("support") | (HasRole("agent") & HasScope("t"))).describe() == (
        "role:support OR (role:agent AND scope:t)"
    )


def test_the_failed_rule_is_named_in_the_403() -> None:
    requirement = HasRole("admin") | HasScope("orders:write")
    response = _client(requirement, _claims(roles=["viewer"])).get(
        "/guarded", headers={"Authorization": "Bearer tok"}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Insufficient permissions; requires: role:admin OR scope:orders:write"
    )
