"""Lifespan composer: startup order, reverse shutdown, rollback on failure."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from pycommon.runtime import LifespanResource, build_lifespan


def _resource(name: str, events: list[str], *, fail_startup: bool = False) -> LifespanResource:
    async def startup() -> None:
        if fail_startup:
            raise RuntimeError(f"{name} failed")
        events.append(f"start:{name}")

    async def shutdown() -> None:
        events.append(f"stop:{name}")

    return LifespanResource(name=name, startup=startup, shutdown=shutdown)


async def test_startup_in_order_shutdown_reversed() -> None:
    events: list[str] = []
    lifespan = build_lifespan([_resource("db", events), _resource("cache", events)])

    async with lifespan(FastAPI()):
        assert events == ["start:db", "start:cache"]

    assert events == ["start:db", "start:cache", "stop:cache", "stop:db"]


async def test_startup_failure_unwinds_started_resources() -> None:
    events: list[str] = []
    lifespan = build_lifespan(
        [
            _resource("db", events),
            _resource("broken", events, fail_startup=True),
            _resource("never", events),
        ]
    )

    with pytest.raises(RuntimeError, match="broken failed"):
        async with lifespan(FastAPI()):
            pass

    # Only the successfully started resource is stopped; "never" is untouched.
    assert events == ["start:db", "stop:db"]


async def test_shutdown_error_does_not_block_others() -> None:
    events: list[str] = []

    async def failing_shutdown() -> None:
        raise RuntimeError("boom")

    bad = LifespanResource(
        name="bad",
        startup=_resource("unused", []).startup,
        shutdown=failing_shutdown,
    )
    lifespan = build_lifespan([_resource("db", events), bad])

    async with lifespan(FastAPI()):
        pass

    assert "stop:db" in events


# --- the gRPC server the lifespan also owns -------------------------------
#
# A service that speaks both HTTP and gRPC hangs its gRPC server off the same
# lifespan, so the ordering guarantees have to hold for it too: started after
# the resources it depends on, stopped before them.


class _FakeGrpcServer:
    """Stands in for GrpcServer — the lifespan only ever calls start/stop."""

    def __init__(self, events: list[str], *, fail_stop: bool = False) -> None:
        self._events = events
        self._fail_stop = fail_stop

    async def start(self) -> None:
        self._events.append("start:grpc")

    async def stop(self) -> None:
        if self._fail_stop:
            raise RuntimeError("grpc refused to stop")
        self._events.append("stop:grpc")


async def test_grpc_server_starts_after_resources_and_stops_before_them() -> None:
    """gRPC handlers use the database and the cache, so it must not accept a
    call before they are up, nor keep accepting after they are gone."""
    events: list[str] = []
    app = FastAPI()
    grpc = _FakeGrpcServer(events)
    lifespan = build_lifespan([_resource("db", events)], grpc_server=grpc)

    async with lifespan(app):
        assert events == ["start:db", "start:grpc"]
        # Exposed so handlers and tests can reach the running server.
        assert app.state.grpc_server is grpc

    assert events == ["start:db", "start:grpc", "stop:grpc", "stop:db"]


async def test_grpc_server_is_not_stopped_when_it_never_started() -> None:
    """A resource failing before gRPC's turn must not produce a stop() on a
    server that was never started."""
    events: list[str] = []
    grpc = _FakeGrpcServer(events)
    lifespan = build_lifespan([_resource("broken", events, fail_startup=True)], grpc_server=grpc)

    with pytest.raises(RuntimeError, match="broken failed"):
        async with lifespan(FastAPI()):
            pass

    assert events == []


async def test_a_failing_grpc_stop_does_not_block_resource_shutdown() -> None:
    """Shutdown is a sequence of best-effort steps; one refusing must not strand
    a database connection pool."""
    events: list[str] = []
    lifespan = build_lifespan(
        [_resource("db", events)],
        grpc_server=_FakeGrpcServer(events, fail_stop=True),
    )

    async with lifespan(FastAPI()):
        pass

    assert events == ["start:db", "start:grpc", "stop:db"]


async def test_a_resource_without_a_shutdown_hook_is_skipped() -> None:
    """``shutdown`` is optional: plenty of resources only need starting."""
    events: list[str] = []

    async def startup() -> None:
        events.append("start:readonly")

    lifespan = build_lifespan(
        [
            LifespanResource(name="readonly", startup=startup),
            _resource("db", events),
        ]
    )

    async with lifespan(FastAPI()):
        pass

    assert events == ["start:readonly", "start:db", "stop:db"]
