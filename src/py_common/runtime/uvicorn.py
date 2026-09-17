"""Reusable uvicorn entrypoint wrapper, with connection draining."""

from __future__ import annotations

import asyncio
import socket
from types import FrameType

import uvicorn

from py_common.config import ServerSettings
from py_common.lifecycle import begin_draining
from py_common.logging import get_logger

logger = get_logger(__name__)


class DrainingServer(uvicorn.Server):
    """A uvicorn server that keeps serving for a while after the shutdown signal.

    Kubernetes removes a pod from its Service endpoints and sends SIGTERM at the
    same time, and the removal then has to propagate to kube-proxy and to every
    ingress controller. A process that begins shutting down the moment it is
    signalled therefore stops accepting connections while traffic is still being
    routed to it, and those requests fail — the connection-refused errors that
    show up as a blip on every deploy.

    On the first signal this marks the process as draining, so readiness starts
    answering 503, and *keeps serving normally* for ``drain_delay_seconds``.
    Only then does uvicorn's own graceful shutdown begin: stop accepting, finish
    in-flight requests, run lifespan shutdown.

    Note this cannot live in the lifespan's shutdown hook, which is the obvious
    place to look for it. By the time lifespan shutdown runs, uvicorn has
    already closed the listening socket, so sleeping there delays cleanup
    without draining anything.

    A second signal skips the wait: an operator pressing Ctrl-C twice, or a
    kubelet escalating, means now rather than later.
    """

    def __init__(self, config: uvicorn.Config, *, drain_delay_seconds: float) -> None:
        super().__init__(config)
        self._drain_delay_seconds = drain_delay_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._draining = False

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        self._loop = asyncio.get_running_loop()
        await super().serve(sockets)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if self._draining or self._loop is None:
            # Second signal, or one arriving before the loop exists. Either way
            # hand straight to uvicorn, which escalates to a forced exit.
            super().handle_exit(sig, frame)
            return

        self._draining = True
        begin_draining()
        logger.info(
            "drain_delay_started",
            signal=sig,
            drain_delay_seconds=self._drain_delay_seconds,
            detail="readiness now fails; still serving traffic",
        )
        # Signal handlers run between bytecodes in the main thread, so the work
        # is handed to the loop rather than done here.
        self._loop.call_soon_threadsafe(self._schedule_exit, sig, frame)

    def _schedule_exit(self, sig: int, frame: FrameType | None) -> None:
        assert self._loop is not None
        self._loop.create_task(self._drain_then_exit(sig, frame))

    async def _drain_then_exit(self, sig: int, frame: FrameType | None) -> None:
        await asyncio.sleep(self._drain_delay_seconds)
        logger.info("drain_delay_finished", detail="beginning graceful shutdown")
        super().handle_exit(sig, frame)


def run_from_settings(
    app_import_string: str,
    settings: ServerSettings,
    *,
    reload: bool = False,
    log_config: dict[str, object] | None = None,
    **kwargs: object,
) -> None:
    """Run uvicorn from :class:`~py_common.config.ServerSettings`.

    The whole point of the settings group: host, port, proxy trust and drain
    delay are deployment facts, and reading them here means an operator changes
    ``SERVER__DRAIN_DELAY_SECONDS`` instead of asking for a release.
    """
    run_uvicorn(
        app_import_string,
        host=settings.host,
        port=settings.port,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        drain_delay_seconds=settings.drain_delay_seconds,
        reload=reload,
        log_config=log_config,
        **kwargs,
    )


def run_uvicorn(
    app_import_string: str,
    *,
    host: str = "0.0.0.0",
    port: int = 8000,
    reload: bool = False,
    forwarded_allow_ips: str | list[str] | None = None,
    log_config: dict[str, object] | None = None,
    drain_delay_seconds: float = 0.0,
    **kwargs: object,
) -> None:
    """Run uvicorn with structlog-friendly defaults (log_config=None disables uvicorn's logging).

    ``forwarded_allow_ips`` lists the peers whose ``X-Forwarded-*`` headers are
    trusted. Leave it ``None`` to keep uvicorn's own resolution (the
    ``FORWARDED_ALLOW_IPS`` environment variable, else ``127.0.0.1``).

    Set it when running behind a reverse proxy. The ``127.0.0.1`` default never
    matches a Kubernetes ingress pod, so until it is set uvicorn ignores the
    proxy headers and three things quietly go wrong: ``scope["scheme"]`` stays
    ``http`` and HSTS is never emitted, and every anonymous caller shares the
    ingress address in access logs and rate-limit buckets. See "Deploying behind
    a proxy" in the README.

    ``drain_delay_seconds`` keeps the server accepting traffic for that long
    after SIGTERM, with readiness failing, so load balancers can take the
    instance out of rotation before it stops listening (see
    :class:`DrainingServer`). Off by default because the right value depends on
    how fast your ingress reacts; 5 to 15 seconds suits most Kubernetes setups,
    and it must be shorter than ``terminationGracePeriodSeconds`` or the kubelet
    will SIGKILL mid-drain. A Kubernetes ``preStop`` hook achieves the same
    delay without application code — this exists for services that cannot set
    one, and it additionally makes ``/health/ready`` tell the truth while
    draining.
    """
    if drain_delay_seconds <= 0:
        uvicorn.run(
            app_import_string,
            host=host,
            port=port,
            reload=reload,
            forwarded_allow_ips=forwarded_allow_ips,
            log_config=log_config,
            **kwargs,  # type: ignore[arg-type]
        )
        return

    if reload:
        # reload runs a supervisor that respawns the worker; the worker's
        # handle_exit is not what receives the signal, so draining would look
        # configured and do nothing. Reload is for development, draining is for
        # production, and no deployment needs both.
        raise ValueError("drain_delay_seconds cannot be combined with reload=True")

    config = uvicorn.Config(
        app_import_string,
        host=host,
        port=port,
        forwarded_allow_ips=forwarded_allow_ips,
        log_config=log_config,
        **kwargs,  # type: ignore[arg-type]
    )
    server = DrainingServer(config, drain_delay_seconds=drain_delay_seconds)
    asyncio.run(server.serve())
