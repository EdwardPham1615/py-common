"""A real prefork worker, as in production (~7 s per start method).

Eager mode runs the task wrapper in the test process and so cannot see the two ways a
pool child fails quietly. This starts an actual worker in a subprocess on a
filesystem broker (no Redis needed), sends it a task, and checks by *effect* -- a flag
file the task writes -- and by the child's output.

* On macOS pool children are **spawned**, not forked, and a spawned child lacks the
  trace setup the main process did, so every task failed before its own code ran
  (``not enough values to unpack``). ``FORKED_BY_MULTIPROCESSING`` makes the child
  redo it. Forcing ``spawn`` here makes Linux CI exercise the macOS path.
* Celery redirects a child's ``sys.stdout`` into logging. A library logger writing to
  ``sys.stdout`` then loops its records back into the redirect, which drops them, so a
  child logged nothing after its first task. Looked like "the task did not run". The
  probe task configures logging itself, the way a lazy service would, to provoke it;
  what keeps it away is ``Worker`` owning logging through ``setup_logging``, which makes
  Celery skip the redirect.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SUCCEEDED = re.compile(r"probe\.touch\[[0-9a-f-]+\] succeeded")


def _probe(command: str, env: dict[str, str], log: int | None = None) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - fixed argv: this interpreter, our own module
        [sys.executable, "-m", "worker_probe", command],
        cwd=TESTS,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT if log is not None else None,
    )


@pytest.mark.parametrize("start_method", ["spawn", "fork"])
def test_a_real_pool_child_runs_the_task_and_logs_it_as_ecs(
    tmp_path: Path, start_method: str
) -> None:
    env = {**os.environ, "PROBE_DIR": str(tmp_path), "PROBE_START_METHOD": start_method}
    # The child must prove the library sets it, not inherit it from this test process.
    env.pop("FORKED_BY_MULTIPROCESSING", None)
    env["PYTHONPATH"] = str(TESTS)
    log_path = tmp_path / "worker.log"

    with log_path.open("wb") as log:
        worker = _probe("worker", env, log.fileno())
        try:
            assert _probe("send", env).wait(timeout=60) == 0
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if (tmp_path / "flag").exists() and SUCCEEDED.search(log_path.read_text()):
                    break
                time.sleep(0.2)
        finally:
            worker.terminate()
            try:
                worker.wait(timeout=30)
            except subprocess.TimeoutExpired:
                worker.kill()

    output = log_path.read_text()
    assert (tmp_path / "flag").exists(), f"the task never ran:\n{output[-2500:]}"
    assert SUCCEEDED.search(output), f"the task ran but its log line is missing:\n{output[-2500:]}"

    lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    succeeded = [rec for rec in lines if "succeeded" in str(rec.get("message", ""))]
    assert succeeded, f"Celery's lines are not ECS JSON:\n{output[-2500:]}"
    assert "ecs.version" in succeeded[0]
    assert succeeded[0]["service"]["name"] == "app-worker"


def _stop(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()


def _wait_until(condition: Callable[[], bool], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not condition():
        time.sleep(0.2)


def test_beat_logs_under_its_own_service_name(tmp_path: Path) -> None:
    """Celery configures logging before it announces beat, so a line is only named
    ``-beat`` once beat_init has run; beat's schedule is due every second, and the line
    it logs for a due task comes after."""
    env = {**os.environ, "PROBE_DIR": str(tmp_path), "PYTHONPATH": str(TESTS)}
    log_path = tmp_path / "beat.log"

    with log_path.open("wb") as log:
        beat = _probe("beat", env, log.fileno())
        try:
            _wait_until(lambda: "Sending due task" in log_path.read_text(), 45)
        finally:
            _stop(beat)

    output = log_path.read_text()
    due = [
        json.loads(line)
        for line in output.splitlines()
        if line.startswith("{") and "Sending due task" in line
    ]
    assert due, f"beat never logged a due task as ECS JSON:\n{output[-2500:]}"
    assert due[0]["service"]["name"] == "app-beat"
