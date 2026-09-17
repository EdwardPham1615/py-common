"""Reusable server runtime: FastAPI shell, lifespan composition, gRPC, uvicorn.

Re-exports resolve lazily, and that is load-bearing rather than an
optimisation. Python executes this file whenever *anything* imports one of its
submodules, so eager re-exports made this package's dependencies everyone's
dependencies: ``import py_common.persistence`` failed without FastAPI, because
``persistence.engine`` reaches in here for ``LifespanResource`` and that pulled
``app.py`` behind it. ``import py_common.runtime`` failed without the
``telemetry`` extra for the same reason, by way of ``grpc_metrics``. Both
shipped in 0.2.0.

``from py_common.runtime import create_base_app`` still works exactly as
before; the submodule is imported the first time the name is read, so a caller
only pays for what it actually touches.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from py_common.runtime.app import create_base_app as create_base_app
    from py_common.runtime.grpc import GrpcServer as GrpcServer
    from py_common.runtime.grpc import ServicerRegistrar as ServicerRegistrar
    from py_common.runtime.grpc import default_otel_interceptors as default_otel_interceptors
    from py_common.runtime.grpc_client import GrpcChannelPool as GrpcChannelPool
    from py_common.runtime.grpc_client import (
        default_otel_client_interceptors as default_otel_client_interceptors,
    )
    from py_common.runtime.grpc_interceptors import (
        RequestIdClientInterceptor as RequestIdClientInterceptor,
    )
    from py_common.runtime.grpc_interceptors import (
        RequestIdServerInterceptor as RequestIdServerInterceptor,
    )
    from py_common.runtime.grpc_interceptors import (
        RequestIdStreamStreamClientInterceptor as RequestIdStreamStreamClientInterceptor,
    )
    from py_common.runtime.grpc_interceptors import (
        RequestIdStreamUnaryClientInterceptor as RequestIdStreamUnaryClientInterceptor,
    )
    from py_common.runtime.grpc_interceptors import (
        RequestIdUnaryStreamClientInterceptor as RequestIdUnaryStreamClientInterceptor,
    )
    from py_common.runtime.grpc_interceptors import (
        request_id_client_interceptors as request_id_client_interceptors,
    )
    from py_common.runtime.grpc_interceptors import (
        request_id_server_interceptors as request_id_server_interceptors,
    )
    from py_common.runtime.grpc_metrics import MetricsServerInterceptor as MetricsServerInterceptor
    from py_common.runtime.grpc_metrics import (
        metrics_server_interceptors as metrics_server_interceptors,
    )
    from py_common.runtime.lifespan import LifespanResource as LifespanResource
    from py_common.runtime.lifespan import build_lifespan as build_lifespan
    from py_common.runtime.uvicorn import DrainingServer as DrainingServer
    from py_common.runtime.uvicorn import run_from_settings as run_from_settings
    from py_common.runtime.uvicorn import run_uvicorn as run_uvicorn

# name -> the submodule that defines it. Grouped by submodule so the extra each
# one costs is visible: `app` and `uvicorn` need `http`/`runtime`, the `grpc_*`
# ones need `grpc`, `grpc_metrics` additionally needs `telemetry`, and
# `lifespan` needs nothing beyond the always-installed core.
_LAZY: dict[str, str] = {
    "create_base_app": "py_common.runtime.app",
    "GrpcServer": "py_common.runtime.grpc",
    "ServicerRegistrar": "py_common.runtime.grpc",
    "default_otel_interceptors": "py_common.runtime.grpc",
    "GrpcChannelPool": "py_common.runtime.grpc_client",
    "default_otel_client_interceptors": "py_common.runtime.grpc_client",
    "RequestIdClientInterceptor": "py_common.runtime.grpc_interceptors",
    "RequestIdServerInterceptor": "py_common.runtime.grpc_interceptors",
    "RequestIdStreamStreamClientInterceptor": "py_common.runtime.grpc_interceptors",
    "RequestIdStreamUnaryClientInterceptor": "py_common.runtime.grpc_interceptors",
    "RequestIdUnaryStreamClientInterceptor": "py_common.runtime.grpc_interceptors",
    "request_id_client_interceptors": "py_common.runtime.grpc_interceptors",
    "request_id_server_interceptors": "py_common.runtime.grpc_interceptors",
    "MetricsServerInterceptor": "py_common.runtime.grpc_metrics",
    "metrics_server_interceptors": "py_common.runtime.grpc_metrics",
    "LifespanResource": "py_common.runtime.lifespan",
    "build_lifespan": "py_common.runtime.lifespan",
    "DrainingServer": "py_common.runtime.uvicorn",
    "run_from_settings": "py_common.runtime.uvicorn",
    "run_uvicorn": "py_common.runtime.uvicorn",
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
