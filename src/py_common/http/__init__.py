"""HTTP helpers: RFC 9457 Problem Details, API response envelope, pagination, health, client."""

from py_common.http.client import CircuitBreakerTransport, create_http_client
from py_common.http.health import HealthCheck, build_health_router
from py_common.http.pagination import Page, PageMeta, decode_cursor, encode_cursor
from py_common.http.problem import (
    ProblemDetail,
    app_error_handler,
    build_problem_types_router,
    http_exception_handler,
    problem_response,
    register_exception_handlers,
    unhandled_exception_handler,
    unhandled_problem_response,
    validation_exception_handler,
)
from py_common.http.response import ApiResponse, Pagination

__all__ = [
    "ApiResponse",
    "CircuitBreakerTransport",
    "HealthCheck",
    "Page",
    "PageMeta",
    "Pagination",
    "ProblemDetail",
    "app_error_handler",
    "build_health_router",
    "build_problem_types_router",
    "create_http_client",
    "decode_cursor",
    "encode_cursor",
    "http_exception_handler",
    "problem_response",
    "register_exception_handlers",
    "unhandled_exception_handler",
    "unhandled_problem_response",
    "validation_exception_handler",
]
