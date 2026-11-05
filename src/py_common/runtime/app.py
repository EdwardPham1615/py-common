"""Shared FastAPI application factory helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI


def create_base_app(
    *,
    title: str,
    version: str,
    lifespan: Callable[..., Any] | None = None,
    debug: bool = False,
    is_dev: bool = False,
    fastapi_native: bool = False,
    **kwargs: Any,
) -> FastAPI:
    """Create a FastAPI app with standard docs URL conventions for non-dev environments.

    ``fastapi_native=True`` configures FastAPI's built-in OpenTelemetry for use beside
    ``setup_telemetry(..., fastapi_native=True)`` -- its duplicate metrics off, probe
    paths excluded. A ``telemetry=`` you pass yourself wins. A FastAPI older than
    0.142 has no such option and is left as it is; ``setup_telemetry`` then falls back
    to the instrumentor.
    """
    show_docs = debug or is_dev
    if fastapi_native:
        from py_common.telemetry import fastapi_native_config, fastapi_native_supported

        if fastapi_native_supported():
            kwargs.setdefault("telemetry", fastapi_native_config())
    return FastAPI(
        title=title,
        version=version,
        lifespan=lifespan,
        docs_url="/docs" if show_docs else None,
        redoc_url="/redoc" if show_docs else None,
        **kwargs,
    )
