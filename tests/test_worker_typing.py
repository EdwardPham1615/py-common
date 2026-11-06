"""``mypy --strict`` accepts a service's own worker settings and runtime classes.

A type error is invisible to the rest of the suite -- the code runs fine -- and was
only ever found by a consumer, as three ``type: ignore`` comments. So the check is a
test: it runs mypy over ``typing_cases/worker_subclass.py`` and expects a clean result.
"""

from __future__ import annotations

from pathlib import Path

from mypy import api

ROOT = Path(__file__).resolve().parent.parent


def test_a_subclass_of_worker_settings_type_checks_without_ignores() -> None:
    case = ROOT / "tests" / "typing_cases" / "worker_subclass.py"
    assert "type: ignore" not in case.read_text(), "the case must not hide the errors it tests"

    stdout, stderr, status = api.run(
        ["--config-file", str(ROOT / "pyproject.toml"), "--no-incremental", str(case)]
    )

    assert status == 0, stdout + stderr
