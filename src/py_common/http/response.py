"""Standard API response envelope for service endpoints."""

from __future__ import annotations

import time
from typing import Any, Self

from pydantic import BaseModel, Field

from py_common.errors import ErrorCode
from py_common.logging import current_request_id


class Pagination(BaseModel):
    """Cursor- and offset-style pagination metadata."""

    next_cursor: str | None = None
    prev_cursor: str | None = None
    size: int | None = None
    has_next: bool | None = None
    has_prev: bool | None = None
    offset: int | None = None
    limit: int | None = None
    page: int | None = None
    total_items: int | None = None


class ApiResponse(BaseModel):
    """Common success/error envelope for service responses.

    ``request_id`` defaults to the value bound for the current request
    (via ``RequestContextMiddleware`` / structlog contextvars). Pass an
    explicit value only when you need to override it.
    """

    request_id: str | None = Field(default_factory=current_request_id)
    code: int = int(ErrorCode.OK)
    message: str = "OK"
    server_time: int = Field(default_factory=lambda: int(time.time()))
    count: int | None = None
    data: Any = None
    agg: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None
    pagination: Pagination | None = None
    error_code: str | None = None
    error_msg: Any = None

    @classmethod
    def ok(
        cls,
        data: Any = None,
        *,
        request_id: str | None = None,
        count: int | None = None,
        pagination: Pagination | None = None,
        meta: dict[str, Any] | None = None,
        agg: dict[str, Any] | None = None,
        message: str = "OK",
    ) -> Self:
        return cls(
            **_with_optional_request_id(
                request_id,
                code=int(ErrorCode.OK),
                message=message,
                data=data,
                count=count,
                pagination=pagination,
                meta=meta,
                agg=agg,
            )
        )

    @classmethod
    def server_error(
        cls,
        message: str = "Server Error",
        *,
        request_id: str | None = None,
        data: Any = None,
        error_code: str | None = None,
        error_msg: Any = None,
    ) -> Self:
        return cls(
            **_with_optional_request_id(
                request_id,
                code=int(ErrorCode.SERVER),
                message=message,
                data=data,
                error_code=error_code,
                error_msg=error_msg,
            )
        )

    @classmethod
    def database_error(
        cls,
        message: str = "Database Error",
        *,
        request_id: str | None = None,
        data: Any = None,
        error_code: str | None = None,
        error_msg: Any = None,
    ) -> Self:
        return cls(
            **_with_optional_request_id(
                request_id,
                code=int(ErrorCode.DATABASE),
                message=message,
                data=data,
                error_code=error_code,
                error_msg=error_msg,
            )
        )

    @classmethod
    def input_error(
        cls,
        message: str = "Input Error",
        *,
        request_id: str | None = None,
        data: Any = None,
        error_code: str | None = None,
        error_msg: Any = None,
    ) -> Self:
        return cls(
            **_with_optional_request_id(
                request_id,
                code=int(ErrorCode.INPUT),
                message=message,
                data=data,
                error_code=error_code,
                error_msg=error_msg,
            )
        )

    @classmethod
    def auth_error(
        cls,
        message: str = "Authorization Error",
        *,
        request_id: str | None = None,
        data: Any = None,
        error_code: str | None = None,
        error_msg: Any = None,
    ) -> Self:
        return cls(
            **_with_optional_request_id(
                request_id,
                code=int(ErrorCode.AUTH),
                message=message,
                data=data,
                error_code=error_code,
                error_msg=error_msg,
            )
        )

    @classmethod
    def app_check_error(
        cls,
        message: str = "App Check Error",
        *,
        request_id: str | None = None,
        data: Any = None,
        error_code: str | None = None,
        error_msg: Any = None,
    ) -> Self:
        return cls(
            **_with_optional_request_id(
                request_id,
                code=int(ErrorCode.APP_CHECK),
                message=message,
                data=data,
                error_code=error_code,
                error_msg=error_msg,
            )
        )


def _with_optional_request_id(
    request_id: str | None,
    **fields: Any,
) -> dict[str, Any]:
    """Omit ``request_id`` when unset so the model ``default_factory`` can fill it."""
    if request_id is not None:
        fields["request_id"] = request_id
    return fields
