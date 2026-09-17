"""Composable FastAPI lifespan with ordered startup and safe rollback."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from py_common.logging import get_logger

if TYPE_CHECKING:
    from fastapi import FastAPI

    from py_common.runtime.grpc import GrpcServer

logger = get_logger(__name__)

StartupFn = Callable[[], Awaitable[None]]
ShutdownFn = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class LifespanResource:
    """One infrastructure resource with async startup/shutdown hooks."""

    name: str
    startup: StartupFn
    shutdown: ShutdownFn | None = None


def build_lifespan(
    resources: Sequence[LifespanResource],
    *,
    grpc_server: GrpcServer | None = None,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Build a FastAPI lifespan that starts resources in order and unwinds on failure/shutdown."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        started: list[LifespanResource] = []
        grpc_started = False

        try:
            for resource in resources:
                logger.info("resource_starting", name=resource.name)
                await resource.startup()
                started.append(resource)
                logger.info("resource_started", name=resource.name)

            if grpc_server is not None:
                await grpc_server.start()
                grpc_started = True
                app.state.grpc_server = grpc_server

            yield
        except Exception:
            logger.exception("lifespan_startup_failed")
            raise
        finally:
            if grpc_started and grpc_server is not None:
                try:
                    await grpc_server.stop()
                except Exception:
                    logger.exception("grpc_server_stop_failed")

            for resource in reversed(started):
                if resource.shutdown is None:
                    continue
                try:
                    logger.info("resource_stopping", name=resource.name)
                    await resource.shutdown()
                    logger.info("resource_stopped", name=resource.name)
                except Exception:
                    logger.exception("resource_stop_failed", name=resource.name)

    return lifespan
