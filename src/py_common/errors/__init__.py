"""Shared application errors and RFC 9457 problem-type registry.

Services raise :class:`AppError` from domain/application code; the FastAPI
handlers in ``py_common.http`` translate them into Problem Details responses
with a stable ``type`` URI and an ``error_code`` extension.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Self


class ErrorCode(IntEnum):
    """Application-level error codes shared across services."""

    OK = 0
    SERVER = 1
    DATABASE = 2
    INPUT = 3
    AUTH = 4
    APP_CHECK = 5
    FORBIDDEN = 6
    NOT_FOUND = 7
    CONFLICT = 8
    RATE_LIMIT = 9
    TIMEOUT = 10
    PAYLOAD_TOO_LARGE = 11
    IDEMPOTENCY = 12


@dataclass(frozen=True, slots=True)
class ProblemType:
    """Canonical RFC 9457 problem-type metadata for an :class:`ErrorCode`."""

    code: ErrorCode
    slug: str
    title: str
    status_code: int
    description: str


PROBLEM_TYPES: dict[ErrorCode, ProblemType] = {
    ErrorCode.SERVER: ProblemType(
        code=ErrorCode.SERVER,
        slug="server",
        title="Server Error",
        status_code=500,
        description=(
            "An unexpected failure occurred while processing the request. "
            "Retry may help for transient faults; otherwise contact support with the request ID."
        ),
    ),
    ErrorCode.DATABASE: ProblemType(
        code=ErrorCode.DATABASE,
        slug="database",
        title="Database Error",
        status_code=500,
        description=(
            "A persistence-layer failure prevented completing the operation. "
            "The request was not fully applied; retry only if the operation is idempotent."
        ),
    ),
    ErrorCode.INPUT: ProblemType(
        code=ErrorCode.INPUT,
        slug="input",
        title="Input Error",
        status_code=400,
        description=(
            "The request was rejected because of invalid, missing, or conflicting input. "
            "Fix the request parameters or body and try again."
        ),
    ),
    ErrorCode.AUTH: ProblemType(
        code=ErrorCode.AUTH,
        slug="authorization",
        title="Authorization Error",
        status_code=401,
        description=(
            "Authentication or authorization failed. "
            "Provide a valid credential (or a credential with the required permissions) and retry."
        ),
    ),
    ErrorCode.APP_CHECK: ProblemType(
        code=ErrorCode.APP_CHECK,
        slug="app-check",
        title="App Check Error",
        status_code=403,
        description=(
            "The client failed an application integrity or device attestation check. "
            "Ensure the client is a genuine build of the official app and retry."
        ),
    ),
    ErrorCode.FORBIDDEN: ProblemType(
        code=ErrorCode.FORBIDDEN,
        slug="forbidden",
        title="Forbidden",
        status_code=403,
        description=(
            "The credential is valid but lacks the permissions required for this operation. "
            "Retrying with the same credential will not help; request the missing role first."
        ),
    ),
    ErrorCode.NOT_FOUND: ProblemType(
        code=ErrorCode.NOT_FOUND,
        slug="not-found",
        title="Not Found",
        status_code=404,
        description=(
            "The requested resource does not exist, or the caller is not allowed to know that "
            "it does. Check the identifier in the request path."
        ),
    ),
    ErrorCode.CONFLICT: ProblemType(
        code=ErrorCode.CONFLICT,
        slug="conflict",
        title="Conflict",
        status_code=409,
        description=(
            "The request conflicts with the current state of the resource (duplicate key, "
            "stale version, or concurrent update). Re-read the resource and retry."
        ),
    ),
    ErrorCode.RATE_LIMIT: ProblemType(
        code=ErrorCode.RATE_LIMIT,
        slug="rate-limit",
        title="Too Many Requests",
        status_code=429,
        description=(
            "The client exceeded the allowed request rate. "
            "Wait for the period given in the Retry-After header before retrying."
        ),
    ),
    ErrorCode.IDEMPOTENCY: ProblemType(
        code=ErrorCode.IDEMPOTENCY,
        slug="idempotency",
        title="Idempotency Conflict",
        status_code=409,
        description=(
            "An Idempotency-Key was reused in a way the server cannot resolve: either an "
            "earlier request with the same key is still running, or the same key was sent "
            "with a different request body. Retrying the original request unchanged is safe; "
            "sending different content requires a new key."
        ),
    ),
    ErrorCode.PAYLOAD_TOO_LARGE: ProblemType(
        code=ErrorCode.PAYLOAD_TOO_LARGE,
        slug="payload-too-large",
        title="Content Too Large",
        status_code=413,
        description=(
            "The request body exceeded the size this service accepts. The limit is reported in "
            "the response detail; splitting the payload or using an upload endpoint designed "
            "for large content is the way through it. Retrying unchanged will not help."
        ),
    ),
    ErrorCode.TIMEOUT: ProblemType(
        code=ErrorCode.TIMEOUT,
        slug="timeout",
        title="Gateway Timeout",
        status_code=504,
        description=(
            "The server gave up before the request finished, and the work it was doing was "
            "cancelled. The request may or may not have taken effect before that point, so "
            "retrying a non-idempotent operation risks doing it twice."
        ),
    ),
}

# Reverse mapping for translating framework-raised HTTP errors (``HTTPException``)
# into application error codes. Explicit rather than derived from PROBLEM_TYPES
# because several codes share a status (APP_CHECK and FORBIDDEN are both 403) and
# only one of them may be the default for that status.
_STATUS_TO_ERROR_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.INPUT,
    401: ErrorCode.AUTH,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.INPUT,
    429: ErrorCode.RATE_LIMIT,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    504: ErrorCode.TIMEOUT,
}


def error_code_for_status(status_code: int) -> ErrorCode | None:
    """Best-effort map an HTTP status to an :class:`ErrorCode`.

    Returns ``None`` when no application code fits (e.g. 405, 418). Callers
    should then emit a Problem Details response without an ``error_code``
    extension rather than inventing a misleading one.
    """
    code = _STATUS_TO_ERROR_CODE.get(status_code)
    if code is not None:
        return code
    if status_code >= 500:
        return ErrorCode.SERVER
    return None


def problem_type_uri(code: ErrorCode, *, base_url: str | None = None) -> str:
    """Resolve the RFC 9457 ``type`` URI for an application error code.

    With no ``base_url``, returns a path-absolute URI (``/problems/{slug}``)
    that resolves against the API host. With ``base_url``, returns an absolute
    URI under that prefix (e.g. a public docs portal).
    """
    if code is ErrorCode.OK:
        raise ValueError("ErrorCode.OK has no problem type URI")
    problem = PROBLEM_TYPES[code]
    if base_url:
        return f"{base_url.rstrip('/')}/{problem.slug}"
    return f"/problems/{problem.slug}"


class AppError(Exception):
    """Application error with a fixed HTTP status derived from :class:`ErrorCode`."""

    def __init__(
        self,
        detail: str | None = None,
        *,
        error_code: ErrorCode = ErrorCode.SERVER,
        title: str | None = None,
        type_: str | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        problem = PROBLEM_TYPES.get(error_code)
        self.error_code = error_code
        self.status_code = problem.status_code if problem is not None else 500
        if title is not None:
            self.title = title
        elif problem is not None:
            self.title = problem.title
        else:
            self.title = "Server Error"
        self.type_ = type_
        self.detail = detail
        self.errors = errors
        super().__init__(detail or self.title)

    def resolve_type(self, *, base_url: str | None = None) -> str:
        """Return the Problem Details ``type`` URI for this error."""
        if self.type_ is not None:
            return self.type_
        if self.error_code is ErrorCode.OK:
            return "about:blank"
        return problem_type_uri(self.error_code, base_url=base_url)

    @classmethod
    def server(
        cls,
        detail: str | None = None,
        *,
        title: str | None = None,
        type_: str | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> Self:
        return cls(
            detail,
            error_code=ErrorCode.SERVER,
            title=title,
            type_=type_,
            errors=errors,
        )

    @classmethod
    def database(
        cls,
        detail: str | None = None,
        *,
        title: str | None = None,
        type_: str | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> Self:
        return cls(
            detail,
            error_code=ErrorCode.DATABASE,
            title=title,
            type_=type_,
            errors=errors,
        )

    @classmethod
    def input(
        cls,
        detail: str | None = None,
        *,
        title: str | None = None,
        type_: str | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> Self:
        return cls(
            detail,
            error_code=ErrorCode.INPUT,
            title=title,
            type_=type_,
            errors=errors,
        )

    @classmethod
    def auth(
        cls,
        detail: str | None = None,
        *,
        title: str | None = None,
        type_: str | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> Self:
        return cls(
            detail,
            error_code=ErrorCode.AUTH,
            title=title,
            type_=type_,
            errors=errors,
        )

    @classmethod
    def app_check(
        cls,
        detail: str | None = None,
        *,
        title: str | None = None,
        type_: str | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> Self:
        return cls(
            detail,
            error_code=ErrorCode.APP_CHECK,
            title=title,
            type_=type_,
            errors=errors,
        )

    @classmethod
    def forbidden(
        cls,
        detail: str | None = None,
        *,
        title: str | None = None,
        type_: str | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> Self:
        return cls(
            detail,
            error_code=ErrorCode.FORBIDDEN,
            title=title,
            type_=type_,
            errors=errors,
        )

    @classmethod
    def not_found(
        cls,
        detail: str | None = None,
        *,
        title: str | None = None,
        type_: str | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> Self:
        return cls(
            detail,
            error_code=ErrorCode.NOT_FOUND,
            title=title,
            type_=type_,
            errors=errors,
        )

    @classmethod
    def conflict(
        cls,
        detail: str | None = None,
        *,
        title: str | None = None,
        type_: str | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> Self:
        return cls(
            detail,
            error_code=ErrorCode.CONFLICT,
            title=title,
            type_=type_,
            errors=errors,
        )

    @classmethod
    def rate_limit(
        cls,
        detail: str | None = None,
        *,
        title: str | None = None,
        type_: str | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> Self:
        return cls(
            detail,
            error_code=ErrorCode.RATE_LIMIT,
            title=title,
            type_=type_,
            errors=errors,
        )


__all__ = [
    "PROBLEM_TYPES",
    "AppError",
    "ErrorCode",
    "ProblemType",
    "error_code_for_status",
    "problem_type_uri",
]
