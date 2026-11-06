"""The smallest real worker app, for ``test_worker_pool.py`` (not a test itself).

Lives in its own importable module so that a spawned pool child, which is a fresh
interpreter, finds the task by importing it -- the situation a real service is in.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from py_common.config import (
    BaseAppSettings,
    OtelSettings,
    RedisSettings,
    SnowflakeSettings,
    WorkerSettings,
)
from py_common.utils import new_snowflake_id
from py_common.worker import Worker, WorkerRuntime

PROBE_DIR = Path(os.environ["PROBE_DIR"])
# The filesystem transport does not create its queue folder.
(PROBE_DIR / "broker").mkdir(parents=True, exist_ok=True)
(PROBE_DIR / "control").mkdir(parents=True, exist_ok=True)


class Settings(BaseAppSettings):
    worker: WorkerSettings = WorkerSettings(broker_url="filesystem://")
    # Declared because the runtime wants it; the probe task never touches Redis, and
    # building the client connects to nothing.
    redis: RedisSettings = RedisSettings()
    # The first id of the range the pool's processes share out.
    snowflake: SnowflakeSettings = SnowflakeSettings(
        worker_id=int(os.getenv("PROBE_SNOWFLAKE_BASE", "100"))
    )
    # Tracing only when a test points the probe at a collector.
    otel: OtelSettings = OtelSettings(
        enabled=bool(os.getenv("PROBE_OTLP")),
        exporter_otlp_endpoint=os.getenv("PROBE_OTLP", "http://localhost:4317"),
        metrics_enabled=False,
    )


worker = Worker(
    "probe",
    Settings(_env_file=None),
    include=["worker_probe_app"],
    # Beat's schedule, for the test that reads beat's logs: due every second.
    beat_schedule={"tick": {"task": "probe.touch", "schedule": 1.0}},
    broker_transport_options={
        "data_folder_in": str(PROBE_DIR / "broker"),
        "data_folder_out": str(PROBE_DIR / "broker"),
        # Otherwise kombu drops a `control/` folder into the working directory.
        "control_folder": str(PROBE_DIR / "control"),
    },
)


@worker.task("probe.touch")
async def touch(runtime: WorkerRuntime) -> None:
    # What a service that configures logging lazily, inside its first task, does. The
    # handler it installs captures `sys.stdout` -- which by now is Celery's redirect
    # proxy unless worker_redirect_stdouts is off -- and every later record from this
    # child, including Celery's own "succeeded" line, is dropped.
    from py_common.logging import setup_logging

    setup_logging(service_name="app-worker")
    (PROBE_DIR / "flag").write_text("ran")


def _meet_and_make_ids(expected: int) -> None:
    """Blocking on purpose: the point is to hold this process until the others are here."""
    (PROBE_DIR / f"here-{os.getpid()}").write_text("")
    deadline = time.monotonic() + 30
    while len(list(PROBE_DIR.glob("here-*"))) < expected and time.monotonic() < deadline:
        time.sleep(0.02)
    boundary = (time.time_ns() // 200_000_000 + 1) * 200_000_000
    while time.time_ns() < boundary:
        pass
    made = [new_snowflake_id() for _ in range(300)]
    (PROBE_DIR / f"ids-{os.getpid()}.json").write_text(json.dumps(made))


@worker.task("probe.ids")
async def ids(runtime: WorkerRuntime, expected: int) -> None:
    """Make ids in the same millisecond as the other ``expected`` tasks.

    Each task waits until ``expected`` tasks are running at once -- which, with one task
    per pool process, means every process -- then all of them wait for the same wall-clock
    boundary and issue ids back to back. Processes that share a worker id would then
    produce identical ids; processes with their own never do.
    """
    _meet_and_make_ids(expected)
