"""``python -m worker_probe worker|send`` -- drives ``worker_probe_app`` from a test."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import billiard
from worker_probe_app import worker


def main() -> None:
    command = sys.argv[1]
    if command == "worker":
        # Spawn is what macOS uses; forcing it makes Linux CI exercise the same path.
        # Fork is Linux's default and has traps of its own.
        billiard.set_start_method(os.environ.get("PROBE_START_METHOD", "spawn"), force=True)
        worker.celery.worker_main(
            ["worker", "--pool=prefork", "--concurrency=1", "--loglevel=INFO", "--without-gossip"]
        )
    elif command == "beat":
        # What `celery beat` runs underneath.
        schedule = Path(os.environ["PROBE_DIR"]) / "beat-schedule"
        worker.celery.Beat(loglevel="INFO", schedule=str(schedule)).run()
    elif command == "send":
        worker.celery.send_task("probe.touch")
    elif command == "send-traced":
        # What an API process does: trace itself, open a span, hand the task over.
        from opentelemetry import trace

        from py_common.telemetry import setup_tracing, shutdown_tracing

        setup_tracing(
            service_name=os.environ["SENDER_SERVICE"],
            otlp_endpoint=os.environ["PROBE_OTLP"],
            instrument=("celery",),
        )
        with trace.get_tracer("probe").start_as_current_span("send-job"):
            worker.celery.send_task("probe.touch")
        shutdown_tracing()
    else:
        raise SystemExit(f"unknown command {command!r}")


if __name__ == "__main__":
    main()
