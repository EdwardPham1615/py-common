"""Opt-in pyinstrument profiler middleware (via fastapi_profiler)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from py_common.config.settings import ProfilerSettings
from py_common.logging import get_logger

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = get_logger(__name__)


def enable_profiler(
    app: FastAPI,
    settings: ProfilerSettings,
    *,
    environment: str = "dev",
) -> bool:
    """Attach :class:`PyInstrumentProfilerMiddleware` when enabled.

    Returns ``True`` if the middleware was attached. No-ops (and logs a warning)
    when the ``profiling`` extra is not installed or when ``settings.enabled``
    is False.

    Safety: the dashboard has no built-in auth — it is refused in production
    unless ``settings.allow_dashboard_in_production`` is explicitly True.
    """
    if not settings.enabled:
        return False

    try:
        from fastapi_profiler import PyInstrumentProfilerMiddleware
    except ImportError:
        logger.warning(
            "profiler_unavailable",
            detail="Install py-common[profiling] to enable the profiler",
        )
        return False

    enable_dashboard = settings.enable_dashboard
    block_dashboard = (
        enable_dashboard
        and environment == "production"
        and not settings.allow_dashboard_in_production
    )
    if block_dashboard:
        logger.warning(
            "profiler_dashboard_blocked",
            detail=(
                "Refusing to enable /__profiler__ dashboard in production "
                "(set allow_dashboard_in_production=True to override)"
            ),
        )
        enable_dashboard = False

    filter_paths = list(settings.filter_paths)
    if enable_dashboard and settings.dashboard_path not in filter_paths:
        filter_paths.append(settings.dashboard_path)

    app.add_middleware(
        PyInstrumentProfilerMiddleware,
        server_app=app,
        profiler_sample_rate=settings.sample_rate,
        slow_request_threshold_ms=settings.slow_request_threshold_ms,
        always_profile_errors=settings.always_profile_errors,
        enable_dashboard=enable_dashboard,
        dashboard_path=settings.dashboard_path,
        filter_paths=filter_paths,
        is_print_each_request=False,
        log_format="json",
        enabled=True,
    )
    logger.info(
        "profiler_enabled",
        sample_rate=settings.sample_rate,
        slow_request_threshold_ms=settings.slow_request_threshold_ms,
        dashboard=enable_dashboard,
    )
    return True
