"""Generic gRPC aio server lifecycle — no service-specific stubs."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from grpc import aio

from py_common.logging import get_logger
from py_common.runtime.grpc_interceptors import request_id_server_interceptors
from py_common.runtime.grpc_metrics import metrics_server_interceptors

logger = get_logger(__name__)

ServicerRegistrar = Callable[[aio.Server], None]


def default_otel_interceptors() -> list[aio.ServerInterceptor]:
    """Return OTel aio server interceptors when the instrumentation package is available."""
    try:
        from opentelemetry.instrumentation.grpc import aio_server_interceptor

        return [aio_server_interceptor()]  # type: ignore[no-untyped-call]
    except Exception:
        return []


class GrpcServer:
    """Reusable gRPC aio server. Consumers pass registrars for their own servicers."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        registrars: Sequence[ServicerRegistrar],
        enabled: bool = True,
        interceptors: Sequence[aio.ServerInterceptor] | None = None,
        use_otel_interceptor: bool = True,
        use_request_id_interceptor: bool = True,
        use_metrics_interceptor: bool = True,
        server_credentials: object | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._registrars = list(registrars)
        self._enabled = enabled
        # OTel + request-id interceptors are added alongside custom ones (not
        # replaced by them); disable explicitly via the use_* flags.
        self._interceptors: list[aio.ServerInterceptor] = []
        if use_otel_interceptor:
            self._interceptors.extend(default_otel_interceptors())
        if use_request_id_interceptor:
            self._interceptors.extend(request_id_server_interceptors())
        if use_metrics_interceptor:
            self._interceptors.extend(metrics_server_interceptors())
        self._interceptors.extend(interceptors or [])
        self._credentials = server_credentials
        self._server: aio.Server | None = None

    @property
    def server(self) -> aio.Server | None:
        return self._server

    async def start(self) -> None:
        if not self._enabled:
            logger.info("grpc_server_disabled")
            return

        kwargs: dict[str, object] = {}
        if self._interceptors:
            kwargs["interceptors"] = self._interceptors

        server = aio.server(**kwargs)
        for registrar in self._registrars:
            registrar(server)

        listen = f"{self._host}:{self._port}"
        if self._credentials is not None:
            server.add_secure_port(listen, self._credentials)
        else:
            server.add_insecure_port(listen)
        await server.start()
        self._server = server
        logger.info("grpc_server_started", address=listen, secure=self._credentials is not None)

    async def stop(self, grace: float = 5.0) -> None:
        if self._server is None:
            return
        await self._server.stop(grace)
        self._server = None
        logger.info("grpc_server_stopped")
