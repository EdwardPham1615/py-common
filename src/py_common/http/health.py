"""Liveness/readiness endpoints with pluggable dependency checks (k8s probes)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import anyio
from fastapi import APIRouter, Response, status

from py_common.lifecycle import is_draining
from py_common.logging import get_logger

logger = get_logger(__name__)

CheckFn = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class HealthCheck:
    """One readiness check. ``check`` must raise on failure (e.g. ping the DB)."""

    name: str
    check: CheckFn
    timeout_seconds: float = 5.0


async def _run_check(hc: HealthCheck) -> str | None:
    """Return an error message if the check failed, else None."""
    try:
        with anyio.fail_after(hc.timeout_seconds):
            await hc.check()
        return None
    except TimeoutError:
        return "timeout"
    except Exception as exc:
        return str(exc) or type(exc).__name__


async def _run_checks(checks: Sequence[HealthCheck]) -> dict[str, str | None]:
    """Run every check concurrently, returning ``{name: error or None}``.

    Sequentially, the endpoint's worst case is the *sum* of the timeouts: three
    dependencies at the 5s default is 15 seconds, well past the 1 to 5 seconds a
    Kubernetes readiness probe waits. The probe would time out and mark the pod
    unready before the endpoint could answer that everything is fine, which is
    the opposite of what a readiness check is for. Run concurrently, the worst
    case is the slowest single check.
    """
    errors: dict[str, str | None] = {}

    async def run(hc: HealthCheck) -> None:
        errors[hc.name] = await _run_check(hc)

    async with anyio.create_task_group() as tg:
        for hc in checks:
            tg.start_soon(run, hc)
    return errors


def build_health_router(
    checks: Sequence[HealthCheck] = (),
    *,
    prefix: str = "/health",
) -> APIRouter:
    """Router with ``{prefix}/live`` (always 200) and ``{prefix}/ready`` (runs checks).

    Once the process is draining, ``/ready`` answers 503 without running the
    checks — see the endpoint for why. ``/live`` keeps answering 200 throughout.
    """
    router = APIRouter(prefix=prefix, tags=["health"])

    @router.get("/live")
    async def live() -> dict[str, str]:
        # Deliberately unaffected by draining. Liveness answers "is this process
        # broken", and a draining process is not broken -- it is finishing. If
        # this returned 503 during a drain the kubelet would restart the
        # container mid-shutdown, killing exactly the in-flight requests the
        # drain exists to protect.
        return {"status": "ok"}

    @router.get("/ready")
    async def ready(response: Response) -> dict[str, object]:
        if is_draining():
            # Answer immediately and skip the checks. Their result cannot change
            # the outcome, and a dependency that has already begun shutting down
            # would make this probe slow at the exact moment the load balancer
            # is trying to find out it should stop sending traffic here.
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "draining", "checks": {}}

        errors = await _run_checks(checks)

        results: dict[str, str] = {}
        failed = False
        for hc in checks:
            error = errors[hc.name]
            if error is None:
                results[hc.name] = "ok"
            else:
                failed = True
                results[hc.name] = f"failed: {error}"
                logger.warning("readiness_check_failed", check=hc.name, error=error)

        if failed:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded" if failed else "ok", "checks": results}

    return router
