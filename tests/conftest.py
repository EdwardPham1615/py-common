"""Suite-wide fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _environment_is_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs as ``dev`` unless it says otherwise.

    ``ENVIRONMENT`` has no default: ``resolve_environment`` raises when nothing
    sets it, so a staging service that forgot the variable does not quietly run as
    development. Tests that exercise that, or any other environment, override this
    with their own ``monkeypatch.setenv`` / ``delenv``.
    """
    monkeypatch.setenv("ENVIRONMENT", "dev")
