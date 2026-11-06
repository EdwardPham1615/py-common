"""The worker extra: the runtime, task wrapper, dead letters, locks, logs and signals.

Tasks run through Celery's eager mode (``apply()``): the real task wrapper, the real
signals, the real runtime and its single event loop -- only the broker is absent.
What eager mode cannot show -- a prefork pool, spawned children, a real broker -- is
in ``test_worker_pool.py`` and the integration suite.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from celery import signals
from fakeredis import FakeAsyncRedis

import py_common.worker.worker as worker_module
from py_common.cache.lock import LOCK_KEY_PREFIX
from py_common.config import BaseAppSettings, OtelSettings, RedisSettings, WorkerSettings
from py_common.worker import (
    Worker,
    WorkerRuntime,
    dead_letter_entry,
    list_dead_letters,
    requeue_dead_letters,
    scheduled_lock,
)

_SIGNALS = (
    ("logging", signals.setup_logging),
    ("process_init", signals.worker_process_init),
    ("beat_init", signals.beat_init),
    ("process_shutdown", signals.worker_process_shutdown),
    ("prerun", signals.task_prerun),
    ("postrun", signals.task_postrun),
    ("failure", signals.task_failure),
)


class _Settings(BaseAppSettings):
    worker: WorkerSettings = WorkerSettings(eager=True)
    redis: RedisSettings = RedisSettings()
    otel: OtelSettings = OtelSettings(enabled=False)


class _FakeRuntime(WorkerRuntime):
    """The real runtime with an in-memory Redis, so dead letters and locks work offline."""

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.redis = FakeAsyncRedis(decode_responses=True)


def _settings(**worker: Any) -> _Settings:
    return _Settings(_env_file=None, worker=WorkerSettings(eager=True, **worker))


@pytest.fixture
def make_worker() -> Iterator[Any]:
    """Build Workers and take their signal handlers off again: Celery's signals are
    process-global, and a handler left behind would fire in every later test."""
    built: list[Worker] = []

    def make(name: str = "t", settings: _Settings | None = None, **kwargs: Any) -> Worker:
        kwargs.setdefault("runtime_factory", _FakeRuntime)
        worker = Worker(name, settings or _settings(), **kwargs)
        built.append(worker)
        return worker

    yield make
    for worker in built:
        worker._close()
        for what, signal in _SIGNALS:
            signal.disconnect(dispatch_uid=f"py_common.worker.{worker.name}.{what}")
        # beat_init is registered under its own uid too
        signals.beat_init.disconnect(dispatch_uid=f"py_common.worker.{worker.name}.beat_init")


def test_the_app_is_configured_for_reliable_json_work(make_worker: Any) -> None:
    worker = make_worker(
        settings=_settings(timezone="Asia/Tokyo"),
        task_routes={"slow.*": {"queue": "slow"}},
        beat_schedule={"x": {"task": "t.x", "schedule": 60}},
    )
    conf = worker.celery.conf

    assert conf.task_serializer == "json"
    assert list(conf.accept_content) == ["json"]
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.task_ignore_result is True
    assert conf.timezone == "Asia/Tokyo"
    assert conf.task_routes == {"slow.*": {"queue": "slow"}}
    assert conf.beat_schedule == {"x": {"task": "t.x", "schedule": 60}}
    assert conf.task_always_eager is True


def test_the_app_connects_to_nothing_on_creation(make_worker: Any) -> None:
    """So an API process can import it only to send tasks."""
    worker = make_worker()
    assert worker._runtime is None
    assert worker._traced is False


def test_macos_spawn_fix_is_set_when_the_app_is_built(
    monkeypatch: pytest.MonkeyPatch, make_worker: Any
) -> None:
    monkeypatch.delenv("FORKED_BY_MULTIPROCESSING", raising=False)
    make_worker()
    assert os.environ["FORKED_BY_MULTIPROCESSING"] == "1"


def test_an_operators_value_for_the_spawn_fix_is_kept(
    monkeypatch: pytest.MonkeyPatch, make_worker: Any
) -> None:
    monkeypatch.setenv("FORKED_BY_MULTIPROCESSING", "custom")
    make_worker()
    assert os.environ["FORKED_BY_MULTIPROCESSING"] == "custom"


def test_a_task_runs_its_coroutine_with_the_runtime_on_one_loop(make_worker: Any) -> None:
    worker = make_worker()
    seen: list[tuple[Any, Any]] = []

    @worker.task("t.record")
    async def record(runtime: WorkerRuntime, value: int) -> int:
        import asyncio

        seen.append((runtime, asyncio.get_running_loop()))
        return value * 2

    assert record.apply(args=[21]).get() == 42
    record.apply(args=[1])

    (rt1, loop1), (rt2, loop2) = seen
    assert rt1 is rt2 is worker.runtime()
    assert loop1 is loop2 is worker.runtime().loop


def test_the_runtime_is_built_once_and_only_when_a_task_needs_it(make_worker: Any) -> None:
    built: list[int] = []

    def factory(settings: Any) -> WorkerRuntime:
        built.append(1)
        return _FakeRuntime(settings)

    worker = make_worker(runtime_factory=factory)

    @worker.task("t.noop")
    async def noop(runtime: WorkerRuntime) -> None: ...

    assert built == []
    noop.apply()
    noop.apply()
    assert built == [1]


def test_retry_policy_comes_from_settings_and_can_be_overridden(make_worker: Any) -> None:
    worker = make_worker(settings=_settings(max_retries=7, retry_backoff_seconds=3))

    @worker.task("t.default")
    async def default(runtime: WorkerRuntime) -> None: ...

    @worker.task("t.custom", max_retries=1, backoff_seconds=9)
    async def custom(runtime: WorkerRuntime) -> None: ...

    assert (default.max_retries, default.retry_backoff) == (7, 3)
    assert (custom.max_retries, custom.retry_backoff) == (1, 9)
    assert default.retry_backoff_max == 600
    assert default.retry_jitter is True
    assert default.autoretry_for == (Exception,)


def test_a_task_that_runs_out_of_retries_leaves_a_dead_letter(make_worker: Any) -> None:
    worker = make_worker()

    @worker.task("t.boom", max_retries=0)
    async def boom(runtime: WorkerRuntime, order_id: str) -> None:
        raise ValueError(f"cannot sync {order_id}")

    boom.apply(args=["o-7"])

    runtime = worker.runtime()
    letters = runtime.run(list_dead_letters(runtime.redis, "jobs:failed"))
    assert len(letters) == 1
    assert letters[0]["task"] == "t.boom"
    assert letters[0]["args"] == ["o-7"]
    assert letters[0]["error"] == "ValueError: cannot sync o-7"
    assert letters[0]["failed_at"] > 0


def test_a_success_leaves_no_dead_letter(make_worker: Any) -> None:
    worker = make_worker()

    @worker.task("t.fine")
    async def fine(runtime: WorkerRuntime) -> None: ...

    fine.apply()

    runtime = worker.runtime()
    assert runtime.run(list_dead_letters(runtime.redis, "jobs:failed")) == []


def test_another_apps_failures_are_not_this_apps_dead_letters(make_worker: Any) -> None:
    """Celery's signals are process-wide; a handler that did not check the sender would
    record every app's failures in every app's list."""
    mine = make_worker("mine")
    other = make_worker("other")

    @other.task("o.boom", max_retries=0)
    async def boom(runtime: WorkerRuntime) -> None:
        raise RuntimeError("not mine")

    boom.apply()

    runtime = mine.runtime()
    assert runtime.run(list_dead_letters(runtime.redis, "jobs:failed")) == []


def test_dead_letters_can_be_listed_and_sent_again(make_worker: Any) -> None:
    worker = make_worker()
    runtime = worker.runtime()
    key = "jobs:failed"
    for n in (1, 2, 3):
        push: Any = runtime.redis.rpush(key, dead_letter_entry("t.job", [n], ValueError("x"), 1.0))
        runtime.run(push)

    class _Celery:
        def __init__(self) -> None:
            self.sent: list[tuple[str, list[Any]]] = []

        def send_task(self, name: str, args: list[Any]) -> None:
            self.sent.append((name, args))

    celery = _Celery()
    assert runtime.run(requeue_dead_letters(celery, runtime.redis, key, limit=2)) == 2  # type: ignore[arg-type]
    assert celery.sent == [("t.job", [1]), ("t.job", [2])]
    assert [d["args"] for d in runtime.run(list_dead_letters(runtime.redis, key))] == [[3]]


def test_a_failed_requeue_puts_the_letter_back(make_worker: Any) -> None:
    worker = make_worker()
    runtime = worker.runtime()
    entry = dead_letter_entry("t.job", [1], ValueError("x"), 1.0)
    push: Any = runtime.redis.rpush("jobs:failed", entry)
    runtime.run(push)

    class _BrokerDown:
        def send_task(self, name: str, args: list[Any]) -> None:
            raise ConnectionError("broker is down")

    with pytest.raises(ConnectionError):
        runtime.run(requeue_dead_letters(_BrokerDown(), runtime.redis, "jobs:failed"))  # type: ignore[arg-type]

    assert len(runtime.run(list_dead_letters(runtime.redis, "jobs:failed"))) == 1


def test_a_locked_task_does_not_run_while_another_holds_the_lock(make_worker: Any) -> None:
    worker = make_worker()
    runs: list[str] = []

    @worker.task("t.nightly", lock=True)
    async def nightly(runtime: WorkerRuntime) -> str:
        runs.append("ran")
        return "done"

    runtime = worker.runtime()
    held: Any = runtime.redis.set(f"{LOCK_KEY_PREFIX}job:t.nightly", "someone-else")
    runtime.run(held)

    assert nightly.apply().get() is None
    assert runs == []

    release: Any = runtime.redis.delete(f"{LOCK_KEY_PREFIX}job:t.nightly")
    runtime.run(release)
    assert nightly.apply().get() == "done"
    assert runs == ["ran"]


def test_the_lock_is_released_when_the_job_fails(make_worker: Any) -> None:
    worker = make_worker()
    runtime = worker.runtime()

    async def hold_and_fail() -> None:
        async with scheduled_lock(runtime, "j") as held:
            assert held is True
            raise RuntimeError("job blew up")

    with pytest.raises(RuntimeError, match="job blew up"):
        runtime.run(hold_and_fail())

    async def again() -> bool:
        async with scheduled_lock(runtime, "j") as held:
            return held

    assert runtime.run(again()) is True


def test_task_fields_are_bound_to_log_lines_during_the_task_only(make_worker: Any) -> None:
    worker = make_worker()
    during: dict[str, Any] = {}

    @worker.task("t.logged")
    async def logged(runtime: WorkerRuntime) -> None:
        during.update(structlog.contextvars.get_contextvars())

    result = logged.apply()

    assert during["task"] == {"name": "t.logged", "id": result.id, "retries": 0}
    assert "task" not in structlog.contextvars.get_contextvars()


def test_celery_logging_is_handed_to_this_library(
    monkeypatch: pytest.MonkeyPatch, make_worker: Any
) -> None:
    """Connecting to setup_logging is what stops Celery configuring logging itself, and
    it fires in the main process and in every pool child."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(worker_module, "setup_logging", lambda **kw: calls.append(kw))
    make_worker("logs", json_logs=False)

    signals.setup_logging.send(sender=None)

    assert calls == [
        {
            "level": "INFO",
            "service_name": "app-worker",
            "environment": "dev",
            "json_logs": False,
        }
    ]


def test_tracing_starts_once_per_process_and_only_when_configured(
    monkeypatch: pytest.MonkeyPatch, make_worker: Any
) -> None:
    started: list[dict[str, Any]] = []
    monkeypatch.setattr("py_common.telemetry.setup_tracing", lambda **kw: started.append(kw))
    settings = _Settings(_env_file=None, worker=WorkerSettings(eager=True), otel=OtelSettings())
    worker = make_worker("traced", settings=settings)

    signals.worker_process_init.send(sender=None)
    signals.beat_init.send(sender=None)
    worker.runtime()  # the lazy path must not start it a second time

    assert len(started) == 1
    assert started[0]["service_name"] == "app-worker"
    assert started[0]["environment"] == "dev"


def test_tracing_is_left_alone_when_otel_is_disabled(
    monkeypatch: pytest.MonkeyPatch, make_worker: Any
) -> None:
    started: list[Any] = []
    monkeypatch.setattr("py_common.telemetry.setup_tracing", lambda **kw: started.append(kw))
    make_worker("untraced")  # otel disabled in _settings()

    signals.worker_process_init.send(sender=None)

    assert started == []


def test_a_process_shutdown_closes_the_runtime_and_flushes_tracing(
    monkeypatch: pytest.MonkeyPatch, make_worker: Any
) -> None:
    flushed: list[bool] = []
    monkeypatch.setattr("py_common.telemetry.setup_tracing", lambda **kw: None)
    monkeypatch.setattr("py_common.telemetry.shutdown_tracing", lambda: flushed.append(True))
    settings = _Settings(_env_file=None, worker=WorkerSettings(eager=True), otel=OtelSettings())
    worker = make_worker("closing", settings=settings)
    runtime = worker.runtime()

    signals.worker_process_shutdown.send(sender=None)

    assert worker._runtime is None
    assert runtime.loop.is_closed()
    assert flushed == [True]


def test_a_runtime_without_a_database_says_so() -> None:
    runtime = _FakeRuntime(_settings())

    async def use() -> None:
        async with runtime.uow():
            pass

    with pytest.raises(RuntimeError, match="no database"):
        runtime.run(use())
    runtime.close()


def test_a_dead_letter_entry_is_json_with_what_is_needed_to_send_it_again() -> None:
    entry = json.loads(dead_letter_entry("t.job", (1, "a"), KeyError("k"), 12.5))

    assert entry == {
        "task": "t.job",
        "args": [1, "a"],
        "error": "KeyError: 'k'",
        "failed_at": 12500,
    }


def test_kombu_and_redis_py_agree_about_disconnect() -> None:
    """The worker used to die on the first lost Redis connection: redis-py 7+ calls
    ``Connection.disconnect(error=...)`` and kombu's Redis transport overrode
    ``disconnect`` without taking keywords. Fixed in kombu 5.7.0, which is why that is
    the extra's floor. This reads both installed sources, so a resolution that pairs a
    redis-py that passes ``error=`` with a kombu that cannot take it is caught here
    rather than by a worker dying in production. It matches text, so the real check
    stays restarting Redis under a running worker."""
    import inspect
    import re

    import kombu.transport.redis as kombu_redis
    import redis.connection as redis_connection

    kombu_takes_keywords = bool(
        re.search(r"def disconnect\(self, \*args, \*\*kwargs\)", inspect.getsource(kombu_redis))
    )
    redis_passes_error = bool(
        re.search(r"\.disconnect\([^)]*error=", inspect.getsource(redis_connection))
    )

    assert kombu_takes_keywords or not redis_passes_error


# -- beat has a name of its own ---------------------------------------------------------


def _traced_settings() -> _Settings:
    return _Settings(_env_file=None, worker=WorkerSettings(eager=True), otel=OtelSettings())


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [({}, "app-beat"), ({"beat_service_name": "scheduler"}, "scheduler")],
)
def test_beat_traces_under_its_own_service_name(
    monkeypatch: pytest.MonkeyPatch, make_worker: Any, kwargs: dict[str, str], expected: str
) -> None:
    started: list[dict[str, Any]] = []
    monkeypatch.setattr("py_common.telemetry.setup_tracing", lambda **kw: started.append(kw))
    make_worker("beat-trace", settings=_traced_settings(), **kwargs)

    signals.beat_init.send(sender=None)

    assert [kw["service_name"] for kw in started] == [expected]


def test_beat_logs_under_its_own_service_name_once_it_knows_it_is_beat(
    monkeypatch: pytest.MonkeyPatch, make_worker: Any
) -> None:
    """Celery sets logging up before it announces beat, so the first call carries the
    worker's name and beat_init corrects it."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(worker_module, "setup_logging", lambda **kw: calls.append(kw))
    make_worker("beat-logs")

    signals.setup_logging.send(sender=None)
    signals.beat_init.send(sender=None)

    assert [kw["service_name"] for kw in calls] == ["app-worker", "app-beat"]


def test_a_pool_process_keeps_the_worker_name_after_beat_is_configured_elsewhere(
    monkeypatch: pytest.MonkeyPatch, make_worker: Any
) -> None:
    started: list[dict[str, Any]] = []
    monkeypatch.setattr("py_common.telemetry.setup_tracing", lambda **kw: started.append(kw))
    make_worker("child-trace", settings=_traced_settings())

    signals.worker_process_init.send(sender=None)

    assert [kw["service_name"] for kw in started] == ["app-worker"]
