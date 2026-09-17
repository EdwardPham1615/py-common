"""Process lifecycle state shared by the HTTP and gRPC layers.

Lives at the top level, importable from any install, because both
``py_common.http.health`` and the server runtime need the same answer to "is this
process still accepting traffic" and neither should have to depend on the
other's extra.
"""

from __future__ import annotations

import threading

from py_common.logging import get_logger

__all__ = ["begin_draining", "is_draining", "reset_draining"]

logger = get_logger(__name__)

_lock = threading.Lock()
_draining = False


def begin_draining() -> None:
    """Declare that the process is shutting down and should stop receiving traffic.

    Readiness starts failing immediately; the process keeps serving whatever it
    already has. Idempotent — a second signal during a drain must not restart
    the clock or log twice.
    """
    global _draining
    with _lock:
        if _draining:
            return
        _draining = True
    logger.info("draining_started")


def is_draining() -> bool:
    """Whether :func:`begin_draining` has been called."""
    return _draining


def reset_draining() -> None:
    """Clear the flag. For tests — a process does not un-drain."""
    global _draining
    with _lock:
        _draining = False
