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
    **kwargs: Any,
) -> FastAPI:
    """Create a FastAPI app with standard docs URL conventions for non-dev environments."""
    show_docs = debug or is_dev
    return FastAPI(
        title=title,
        version=version,
        lifespan=lifespan,
        docs_url="/docs" if show_docs else None,
        redoc_url="/redoc" if show_docs else None,
        **kwargs,
    )
