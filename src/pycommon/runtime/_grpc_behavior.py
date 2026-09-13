"""Locating the handler behavior on a gRPC ``RpcMethodHandler``.

Server interceptors that need to run code *around* a handler — rather than
around the handler *lookup* — all have to find which of the four behavior
fields this particular handler uses and rebuild the namedtuple with a wrapper
in its place. Both :mod:`pycommon.runtime.grpc_metrics` and
:mod:`pycommon.runtime.grpc_interceptors` do exactly that, so the mapping lives
here instead of being copied into each of them.

Private to ``pycommon.runtime``; nothing here is re-exported.
"""

from __future__ import annotations

from typing import Any

__all__ = ["BEHAVIOR_FIELD", "behavior_field"]

# (request_streaming, response_streaming) -> the handler field holding the behavior.
BEHAVIOR_FIELD = {
    (False, False): "unary_unary",
    (False, True): "unary_stream",
    (True, False): "stream_unary",
    (True, True): "stream_stream",
}


def behavior_field(handler: Any) -> str | None:
    """Return the name of the field holding ``handler``'s behavior.

    ``None`` when the field is absent — a handler built by hand may leave it
    unset, and there is nothing to wrap in that case.
    """
    field = BEHAVIOR_FIELD[(handler.request_streaming, handler.response_streaming)]
    return field if getattr(handler, field, None) is not None else None
