"""Connection draining: readiness, liveness, and the delayed shutdown."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pycommon.http import HealthCheck, build_health_router
from pycommon.lifecycle import begin_draining, is_draining, reset_draining
from pycommon.runtime.uvicorn import DrainingServer, run_uvicorn


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_draining()
    yield
    reset_draining()


def _client(checks: tuple[HealthCheck, ...] = ()) -> TestClient:
    app = FastAPI()
    app.include_router(build_health_router(checks))
    return TestClient(app)


# --- health endpoints -----------------------------------------------------


def test_ready_is_503_while_draining() -> None:
    client = _client()
    assert client.get("/health/ready").status_code == 200

    begin_draining()
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "draining"


def test_live_stays_200_while_draining() -> None:
    """The one that matters most. If liveness failed during a drain the kubelet
    would restart the container mid-shutdown, killing exactly the in-flight
    requests the drain exists to protect."""
    client = _client()
    begin_draining()

    resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_draining_readiness_does_not_run_the_checks() -> None:
    """Their result cannot change the answer, and a dependency already shutting
    down would make this probe slow at the moment the load balancer is trying to
    learn it should stop sending traffic here."""
    calls = 0

    async def check() -> None:
        nonlocal calls
        calls += 1

    client = _client((HealthCheck(name="db", check=check),))
    client.get("/health/ready")
    assert calls == 1

    begin_draining()
    client.get("/health/ready")
    assert calls == 1  # not called again


def test_begin_draining_is_idempotent() -> None:
    """A second SIGTERM during a drain must not restart anything."""
    begin_draining()
    begin_draining()
    assert is_draining() is True


# --- the server -----------------------------------------------------------


async def _asgi(scope: object, receive: object, send: object) -> None:  # pragma: no cover
    """Minimal ASGI app so uvicorn.Config needs no import string."""


def _server(delay: float) -> DrainingServer:
    """A real DrainingServer through its real constructor — nothing is served,
    only handle_exit is exercised."""
    config = uvicorn.Config(_asgi, log_config=None)
    return DrainingServer(config, drain_delay_seconds=delay)


async def test_first_signal_drains_before_exiting() -> None:
    """The behaviour the whole feature is: readiness fails immediately, the
    server keeps running, and only later does shutdown begin."""
    server = _server(0.2)
    server._loop = asyncio.get_running_loop()

    server.handle_exit(15, None)

    assert is_draining() is True
    assert server.should_exit is False  # still serving

    await asyncio.sleep(0.35)
    assert server.should_exit is True


async def test_second_signal_exits_immediately() -> None:
    """Ctrl-C twice, or a kubelet escalating, means now rather than later."""
    server = _server(30.0)
    server._loop = asyncio.get_running_loop()

    server.handle_exit(15, None)
    assert server.should_exit is False

    server.handle_exit(15, None)
    assert server.should_exit is True  # no 30-second wait


async def test_signal_before_the_loop_exists_exits_immediately() -> None:
    """Signalled during startup: there is nothing serving to drain, and no loop
    to schedule the wait on."""
    server = _server(30.0)
    server._loop = None

    server.handle_exit(15, None)
    assert server.should_exit is True


def test_drain_delay_rejects_reload() -> None:
    """reload runs a supervisor that respawns the worker, so the worker's
    handle_exit never sees the signal — draining would look configured and do
    nothing at all."""
    with pytest.raises(ValueError, match="reload"):
        run_uvicorn("app:app", reload=True, drain_delay_seconds=5.0)


# --- what actually gets run ------------------------------------------------
#
# run_uvicorn picks one of two paths and builds the server for the second one.
# Nothing here binds a port: uvicorn.run and asyncio.run are the boundary, and
# the point is which one is called with what.


def test_no_drain_delay_runs_uvicorn_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default path has to stay the plain one -- a service that never asked
    for draining should not get a DrainingServer and an extra event loop."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app, **kw))
    monkeypatch.setattr(
        "pycommon.runtime.uvicorn.DrainingServer",
        lambda *a, **kw: pytest.fail("DrainingServer must not be built without a drain delay"),
    )

    run_uvicorn("main:app", host="127.0.0.1", port=9001, forwarded_allow_ips="10.0.0.0/8")

    assert captured["app"] == "main:app"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9001
    assert captured["forwarded_allow_ips"] == "10.0.0.0/8"
    # None disables uvicorn's own logging config, which is what keeps structlog
    # the only thing formatting output.
    assert captured["log_config"] is None


def test_drain_delay_builds_a_draining_server(monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict[str, object] = {}
    served: list[object] = []

    class _Recorder(DrainingServer):
        def __init__(self, config: uvicorn.Config, *, drain_delay_seconds: float) -> None:
            super().__init__(config, drain_delay_seconds=drain_delay_seconds)
            built.update(config=config, delay=drain_delay_seconds)

        async def serve(self, sockets: object = None) -> None:
            served.append(self)

    monkeypatch.setattr("pycommon.runtime.uvicorn.DrainingServer", _Recorder)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: pytest.fail("should not be reached"))

    run_uvicorn("main:app", host="127.0.0.1", port=9002, drain_delay_seconds=7.5)

    assert built["delay"] == 7.5
    config = built["config"]
    assert isinstance(config, uvicorn.Config)
    assert config.host == "127.0.0.1"
    assert config.port == 9002
    assert len(served) == 1


async def test_serve_captures_the_loop_it_runs_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """``handle_exit`` runs in a signal handler and hands work to this loop.

    Without it captured, the first signal takes the no-loop path and exits
    immediately -- the drain would be configured and do nothing, which is the
    failure the whole class exists to avoid.
    """

    async def fake_serve(self: object, sockets: object = None) -> None:
        return None

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    server = _server(1.0)
    assert server._loop is None

    await server.serve()

    assert server._loop is asyncio.get_running_loop()


def test_run_from_settings_passes_the_whole_server_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason ServerSettings exists: an operator changes
    SERVER__DRAIN_DELAY_SECONDS instead of asking for a release. That only holds
    if every field actually reaches uvicorn."""
    from pycommon.config import ServerSettings
    from pycommon.runtime.uvicorn import run_from_settings

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "pycommon.runtime.uvicorn.run_uvicorn",
        lambda app, **kw: captured.update(app=app, **kw),
    )

    run_from_settings(
        "main:app",
        ServerSettings(
            host="127.0.0.1",
            port=9003,
            forwarded_allow_ips="10.0.0.0/8",
            drain_delay_seconds=12.0,
        ),
    )

    assert captured == {
        "app": "main:app",
        "host": "127.0.0.1",
        "port": 9003,
        "forwarded_allow_ips": "10.0.0.0/8",
        "drain_delay_seconds": 12.0,
        "reload": False,
        "log_config": None,
    }
