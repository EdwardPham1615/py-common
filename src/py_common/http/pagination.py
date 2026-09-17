"""Pagination envelope supporting both cursor- and offset-based styles."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from pydantic import BaseModel, Field


class PageMeta(BaseModel):
    # Cursor-based
    next_cursor: str | None = None
    prev_cursor: str | None = None
    has_more: bool = False
    # Offset-based
    offset: int | None = None
    page: int | None = None
    total_items: int | None = None

    limit: int = 20


class Page[T](BaseModel):
    items: list[T]
    meta: PageMeta = Field(default_factory=PageMeta)


def encode_cursor(payload: dict[str, Any]) -> str:
    """Encode an opaque pagination cursor (URL-safe base64 JSON)."""
    raw = json.dumps(payload, separators=(",", ":"), default=str).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    """Decode a cursor produced by :func:`encode_cursor`.

    Raises ``ValueError`` on malformed input so callers can map it to a 400.
    """
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode())
        payload = json.loads(raw)
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid pagination cursor") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid pagination cursor")
    return payload
