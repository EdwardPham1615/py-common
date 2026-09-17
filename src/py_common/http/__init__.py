"""HTTP helpers: RFC 9457 Problem Details, API response envelope, pagination, health, client.

Re-exports resolve lazily, for the reason spelled out in
:mod:`py_common.runtime`: Python runs this file whenever anything imports one
of its submodules, so eager re-exports charge every importer for the union of
this package's dependencies. ``py_common.persistence.pagination`` reaches in
here for the cursor codec and was paying for ``httpx`` to do it, which made
``import py_common.persistence`` fail on a plain ``py-common[persistence]``
install. That shipped in 0.2.0.

``from py_common.http import register_exception_handlers`` is unchanged; the
submodule loads the first time the name is read.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from py_common.http.client import CircuitBreakerTransport as CircuitBreakerTransport
    from py_common.http.client import create_http_client as create_http_client
    from py_common.http.health import HealthCheck as HealthCheck
    from py_common.http.health import build_health_router as build_health_router
    from py_common.http.pagination import Page as Page
    from py_common.http.pagination import PageMeta as PageMeta
    from py_common.http.pagination import decode_cursor as decode_cursor
    from py_common.http.pagination import encode_cursor as encode_cursor
    from py_common.http.problem import ProblemDetail as ProblemDetail
    from py_common.http.problem import app_error_handler as app_error_handler
    from py_common.http.problem import build_problem_types_router as build_problem_types_router
    from py_common.http.problem import http_exception_handler as http_exception_handler
    from py_common.http.problem import problem_response as problem_response
    from py_common.http.problem import register_exception_handlers as register_exception_handlers
    from py_common.http.problem import unhandled_exception_handler as unhandled_exception_handler
    from py_common.http.problem import unhandled_problem_response as unhandled_problem_response
    from py_common.http.problem import validation_exception_handler as validation_exception_handler
    from py_common.http.response import ApiResponse as ApiResponse
    from py_common.http.response import Pagination as Pagination

# name -> defining submodule. `pagination` and `response` are pure pydantic and
# cost nothing; `client` needs httpx and `health`/`problem` need FastAPI, which
# is why reaching for a cursor helper must not drag them in.
_LAZY: dict[str, str] = {
    "CircuitBreakerTransport": "py_common.http.client",
    "create_http_client": "py_common.http.client",
    "HealthCheck": "py_common.http.health",
    "build_health_router": "py_common.http.health",
    "Page": "py_common.http.pagination",
    "PageMeta": "py_common.http.pagination",
    "decode_cursor": "py_common.http.pagination",
    "encode_cursor": "py_common.http.pagination",
    "ProblemDetail": "py_common.http.problem",
    "app_error_handler": "py_common.http.problem",
    "build_problem_types_router": "py_common.http.problem",
    "http_exception_handler": "py_common.http.problem",
    "problem_response": "py_common.http.problem",
    "register_exception_handlers": "py_common.http.problem",
    "unhandled_exception_handler": "py_common.http.problem",
    "unhandled_problem_response": "py_common.http.problem",
    "validation_exception_handler": "py_common.http.problem",
    "ApiResponse": "py_common.http.response",
    "Pagination": "py_common.http.response",
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:
    """PEP 562 hook: resolve a re-export on first access."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module), name)


def __dir__() -> list[str]:
    return __all__
