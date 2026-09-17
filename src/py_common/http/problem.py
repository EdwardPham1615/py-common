"""RFC 9457 Problem Details models, handlers, and problem-type docs router."""

from __future__ import annotations

import time
from html import escape
from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from py_common.errors import (
    PROBLEM_TYPES,
    AppError,
    ErrorCode,
    error_code_for_status,
    problem_type_uri,
)
from py_common.logging import current_request_id, get_logger

logger = get_logger(__name__)

APP_STATE_PROBLEM_TYPE_BASE_URL = "problem_type_base_url"


class ProblemDetail(BaseModel):
    """RFC 9457 Problem Details for HTTP APIs.

    Extension members: ``error_code``, ``request_id``, ``server_time``.
    """

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    errors: list[dict[str, Any]] | None = None
    error_code: int | None = None
    request_id: str | None = None
    server_time: int = Field(default_factory=lambda: int(time.time()))


def problem_response(
    *,
    title: str,
    status_code: int,
    detail: str | None = None,
    instance: str | None = None,
    type_: str = "about:blank",
    errors: list[dict[str, Any]] | None = None,
    error_code: int | None = None,
    request_id: str | None = None,
    server_time: int | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ProblemDetail(
        type=type_,
        title=title,
        status=status_code,
        detail=detail,
        instance=instance,
        errors=errors,
        error_code=error_code,
        request_id=request_id,
        server_time=server_time if server_time is not None else int(time.time()),
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(exclude_none=True),
        media_type="application/problem+json",
        headers=headers,
    )


def _problem_type_base_url(request: Request) -> str | None:
    value = getattr(request.app.state, APP_STATE_PROBLEM_TYPE_BASE_URL, None)
    return value if isinstance(value, str) else None


def _request_id(request: Request) -> str | None:
    """Prefer the scope, fall back to the logging context.

    Both are set by ``RequestContextMiddleware``, so they agree when it ran.
    The fallback covers a handler reached without it — where the scope carries
    nothing but a request ID may still be bound, and a Problem Details body
    saying ``request_id: null`` while the logs carry one is the correlation
    gap this endpoint exists to close.
    """
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else current_request_id()


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """Translate a domain :class:`AppError` into a Problem Details response."""
    if exc.status_code >= 500:
        logger.exception("app_error", path=request.url.path, title=exc.title)
    base_url = _problem_type_base_url(request)
    return problem_response(
        title=exc.title,
        status_code=exc.status_code,
        detail=exc.detail,
        instance=str(request.url.path),
        type_=exc.resolve_type(base_url=base_url),
        errors=exc.errors,
        error_code=int(exc.error_code),
        request_id=_request_id(request),
    )


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Translate FastAPI request validation failures into Problem Details.

    Field-level errors land in the ``errors`` extension member, so clients get
    the same detail FastAPI would normally put in ``detail`` — but inside the
    one error envelope this API promises.
    """
    base_url = _problem_type_base_url(request)
    problem = PROBLEM_TYPES[ErrorCode.INPUT]
    return problem_response(
        title=problem.title,
        # Literal rather than status.HTTP_422_*: Starlette renamed the constant
        # (ENTITY -> CONTENT) and we support versions on both sides of that.
        status_code=422,
        detail="Request validation failed",
        instance=str(request.url.path),
        type_=problem_type_uri(ErrorCode.INPUT, base_url=base_url),
        errors=_validation_errors(exc),
        error_code=int(ErrorCode.INPUT),
        request_id=_request_id(request),
    )


def _validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Normalize pydantic errors, dropping the unserializable ``ctx`` payload."""
    errors: list[dict[str, Any]] = []
    for error in exc.errors():
        entry = {k: v for k, v in error.items() if k != "ctx"}
        loc = entry.get("loc")
        if isinstance(loc, (list, tuple)):
            entry["loc"] = [str(part) for part in loc]
        errors.append(entry)
    return errors


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    """Translate ``HTTPException`` (including py_common's own 401/403/429) into Problem Details.

    ``exc.headers`` is preserved — dropping it would strip ``WWW-Authenticate``
    from 401s and ``Retry-After`` from 429s, both of which are protocol-required.
    """
    base_url = _problem_type_base_url(request)
    error_code = error_code_for_status(exc.status_code)
    problem = PROBLEM_TYPES.get(error_code) if error_code is not None else None

    if problem is not None:
        title = problem.title
        type_ = problem_type_uri(problem.code, base_url=base_url)
    else:
        # No application code fits this status — stay RFC 9457 compliant with a
        # generic type rather than inventing a misleading error_code.
        title = HTTPStatus(exc.status_code).phrase
        type_ = "about:blank"

    if exc.status_code >= 500:
        logger.exception("http_exception", path=request.url.path, status_code=exc.status_code)

    return problem_response(
        title=title,
        status_code=exc.status_code,
        detail=str(exc.detail) if exc.detail is not None else None,
        instance=str(request.url.path),
        type_=type_,
        error_code=int(error_code) if error_code is not None else None,
        request_id=_request_id(request),
        headers=dict(exc.headers) if exc.headers else None,
    )


def unhandled_problem_response(
    *,
    path: str,
    request_id: str | None = None,
    base_url: str | None = None,
) -> JSONResponse:
    """Build the canonical 500 Problem Details response.

    Takes plain values instead of a ``Request`` so pure-ASGI middleware can
    render the same body without constructing one. The exception message is
    deliberately not included — internals must not leak to clients.
    """
    return problem_response(
        title="Server Error",
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="An unexpected error occurred",
        instance=path,
        type_=problem_type_uri(ErrorCode.SERVER, base_url=base_url),
        error_code=int(ErrorCode.SERVER),
        request_id=request_id,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort 500 handler.

    Normally unreachable: ``RequestContextMiddleware`` catches unhandled
    exceptions first, so the response still passes back through CORS and the
    security-header middleware. This stays registered for apps that do not
    install that middleware.
    """
    # Once a handler is registered for Exception, Starlette no longer logs the
    # traceback itself — do it here or 500s become invisible.
    logger.exception(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error=str(exc),
    )
    return unhandled_problem_response(
        path=str(request.url.path),
        request_id=_request_id(request),
        base_url=_problem_type_base_url(request),
    )


def register_exception_handlers(
    app: FastAPI,
    *,
    problem_type_base_url: str | None = None,
) -> None:
    """Register the standard handlers so every error response is RFC 9457 Problem Details.

    Covers ``AppError``, request validation failures (422), ``HTTPException``
    (including the 401/403/429 raised by py_common's own auth and rate-limit
    dependencies), and unhandled exceptions.

    ``problem_type_base_url`` prefixes RFC 9457 ``type`` URIs (e.g.
    ``https://docs.example.com/problems``). When omitted, path-absolute URIs
    like ``/problems/input`` are used.
    """
    setattr(app.state, APP_STATE_PROBLEM_TYPE_BASE_URL, problem_type_base_url)
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)


def _problem_type_html(slug: str, *, base_url: str | None = None) -> str | None:
    for problem in PROBLEM_TYPES.values():
        if problem.slug == slug:
            type_uri = problem_type_uri(problem.code, base_url=base_url)
            return (
                "<!DOCTYPE html>\n"
                f'<html lang="en"><head><meta charset="utf-8">'
                f"<title>{escape(problem.title)}</title></head><body>\n"
                f"<h1>{escape(problem.title)}</h1>\n"
                f"<p><strong>type:</strong> <code>{escape(type_uri)}</code></p>\n"
                f"<p><strong>error_code:</strong> {int(problem.code)}</p>\n"
                f"<p><strong>HTTP status:</strong> {problem.status_code}</p>\n"
                f"<p>{escape(problem.description)}</p>\n"
                '<p><a href="/problems">All problem types</a></p>\n'
                "</body></html>\n"
            )
    return None


def _problem_index_html(*, base_url: str | None = None) -> str:
    items = []
    for problem in PROBLEM_TYPES.values():
        type_uri = problem_type_uri(problem.code, base_url=base_url)
        items.append(
            "<li>"
            f'<a href="/problems/{escape(problem.slug)}">{escape(problem.title)}</a>'
            f" — <code>{escape(type_uri)}</code>"
            f" (error_code={int(problem.code)})"
            "</li>"
        )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>Problem Types</title></head><body>\n"
        "<h1>Problem Types</h1>\n"
        "<p>RFC 9457 problem type documentation for this API.</p>\n"
        f"<ul>\n{''.join(items)}\n</ul>\n"
        "</body></html>\n"
    )


def build_problem_types_router(*, problem_type_base_url: str | None = None) -> APIRouter:
    """Serve human-readable HTML docs so relative ``type`` URIs are dereferenceable."""
    router = APIRouter(tags=["problems"])

    @router.get("/problems", response_class=HTMLResponse, include_in_schema=False)
    async def problem_index() -> HTMLResponse:
        return HTMLResponse(_problem_index_html(base_url=problem_type_base_url))

    @router.get("/problems/{slug}", response_class=HTMLResponse, include_in_schema=False)
    async def problem_detail(slug: str) -> HTMLResponse:
        html = _problem_type_html(slug, base_url=problem_type_base_url)
        if html is None:
            return HTMLResponse(
                "<!DOCTYPE html><html><body><h1>Unknown problem type</h1></body></html>",
                status_code=404,
            )
        return HTMLResponse(html)

    return router
