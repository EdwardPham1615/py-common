"""The smallest real worker app, for ``test_worker_pool.py`` (not a test itself).

Lives in its own importable module so that a spawned pool child, which is a fresh
interpreter, finds the task by importing it -- the situation a real service is in.
"""

from __future__ import annotations

import os
from pathlib import Path

from py_common.config import BaseAppSettings, OtelSettings, RedisSettings, WorkerSettings
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
