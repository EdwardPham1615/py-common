"""ID generators: nanoid (URL-safe short IDs) and UUIDv7 (time-ordered PKs)."""

from __future__ import annotations

import os
import secrets
import threading
import time
import uuid

# Alphabet presets for nanoid generation.
ALPHABET_LOWER = "abcdefghijklmnopqrstuvwxyz"
ALPHABET_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
ALPHABET_NUMERIC = "0123456789"
ALPHABET_ALPHA = ALPHABET_LOWER + ALPHABET_UPPER
ALPHABET_ALPHANUMERIC = ALPHABET_ALPHA + ALPHABET_NUMERIC
ALPHABET_UPPER_NUMERIC = ALPHABET_UPPER + ALPHABET_NUMERIC
# Default nanoid alphabet (URL-safe, no lookalikes).
ALPHABET_DEFAULT = "_-0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

DEFAULT_NANOID_SIZE = 21

# Monotonic counter state for same-millisecond UUIDv7 ordering (RFC 9562 Method 1).
_uuid7_lock = threading.Lock()
_uuid7_last_ts_ms = -1
_uuid7_counter = 0


def new_nanoid(
    size: int = DEFAULT_NANOID_SIZE,
    *,
    alphabet: str = ALPHABET_DEFAULT,
) -> str:
    """Generate a cryptographically secure nanoid.

    Uses :mod:`secrets` (no third-party dependency). Suitable for user-facing
    short codes (order refs, invite tokens). Prefer :func:`new_uuid7` for
    database primary keys.
    """
    if size < 1:
        raise ValueError("size must be >= 1")
    if not alphabet:
        raise ValueError("alphabet must not be empty")
    return "".join(secrets.choice(alphabet) for _ in range(size))


def new_uuid7() -> uuid.UUID:
    """Generate a UUIDv7 (time-ordered, sortable) per RFC 9562.

    Prefer this over UUIDv4 for primary keys — inserts stay mostly sequential
    and index fragmentation stays low. Within the same millisecond a 12-bit
    counter keeps values monotonically increasing (RFC 9562 Method 1).
    """
    global _uuid7_last_ts_ms, _uuid7_counter

    with _uuid7_lock:
        ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
        if ts_ms == _uuid7_last_ts_ms:
            _uuid7_counter = (_uuid7_counter + 1) & 0x0FFF
            if _uuid7_counter == 0:
                # Counter overflow — wait for the next millisecond.
                while True:
                    ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
                    if ts_ms != _uuid7_last_ts_ms:
                        break
                _uuid7_counter = 0
        else:
            _uuid7_counter = int.from_bytes(os.urandom(2), "big") & 0x0FFF
        _uuid7_last_ts_ms = ts_ms
        rand_a = _uuid7_counter

    rand_b = int.from_bytes(os.urandom(8), "big") & 0x3FFFFFFFFFFFFFFF
    # Layout: timestamp (48) | ver (4=0111) | rand_a (12) | var (2=10) | rand_b (62)
    value = (ts_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)
