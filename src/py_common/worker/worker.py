"""A Celery application wired to this library's settings, logging, tracing and runtime."""

from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import structlog
from celery import Celery, signals

from py_common.logging import get_logger, setup_logging
from py_common.worker.deadletter import dead_letter_entry
from py_common.worker.runtime import WorkerHost, WorkerRuntime, scheduled_lock

logger = get_logger(__name__)

AsyncJob = Callable[..., Awaitable[Any]]


class Worker:
    """One Celery app, one runtime per worker process, and the plumbing around them.

    ::

        worker = Worker("orders", settings, runtime_factory=OrdersRuntime,
                        include=["orders.tasks"], beat_schedule={...})

        @worker.task("orders.sync", lock=True)
        async def sync(runtime: OrdersRuntime, order_id: str) -> None:
            async with runtime.uow() as uow: ...

    ``worker.celery`` is the app to hand to the ``celery`` command. Creating it
    connects to nothing, so an API process can import it just to *send* tasks.

    What it sets up, each because a worker without it fails quietly:

    * ``FORKED_BY_MULTIPROCESSING``. macOS starts pool children with spawn, which
      leaves them without the trace setup the main process did, so every task fails
      before its own code runs. Setting it makes each child redo that setup.
    * Logging owned by this library, through Celery's ``setup_logging`` signal, in the
      main process and in every child. That signal is also what keeps Celery's stdout
      redirect off: Celery skips the redirect whenever a handler is connected to it
      (measured on 5.6.3: a child's ``sys.stdout`` stays a plain stream even with
      ``worker_redirect_stdouts=True``). Left on, the redirect swallows records
      written to ``sys.stdout``, and a child logs nothing after its first task.
    * Tracing started in each child (``worker_process_init``) and in beat
      (``beat_init``). Under spawn a child inherits no provider, so it has to start its
      own; the lazy path in :meth:`runtime` covers the solo pool and eager mode, which
      never fire that signal. Beat names itself ``<app_name>-beat`` (``beat_service_name``)
      in traces and logs, the pool ``<app_name>-worker``.
    * Late acknowledgement, so a task running in a killed worker goes back on the
      queue. **Every task must therefore be safe to run twice.**
    """

    def __init__(
        self,
        name: str,
        settings: WorkerHost,
        *,
        runtime_factory: Callable[[Any], WorkerRuntime] = WorkerRuntime,
        include: Sequence[str] = (),
        task_routes: Mapping[str, Any] | None = None,
        beat_schedule: Mapping[str, Any] | None = None,
        json_logs: bool = True,
        beat_service_name: str | None = None,
        **conf: Any,
    ) -> None:
        # setdefault: an operator's explicit value wins. Must precede creating the app.
        os.environ.setdefault("FORKED_BY_MULTIPROCESSING", "1")
        self.name = name
        self.settings = settings
        self._runtime_factory = runtime_factory
        self._json_logs = json_logs
        self._runtime: WorkerRuntime | None = None
        self._traced = False
        self._beat = False
        self._beat_service_name = beat_service_name

        worker = settings.worker
        self.celery = Celery(name, broker=worker.broker_url, include=list(include))
        self.celery.conf.update(
            task_serializer="json",
            accept_content=["json"],
            # No result backend: nobody waits for return values.
            task_ignore_result=True,
            task_acks_late=True,
            task_reject_on_worker_lost=True,
            worker_prefetch_multiplier=1,
            timezone=worker.timezone,
            enable_utc=True,
            broker_connection_retry_on_startup=True,
            task_always_eager=worker.eager,
            task_routes=dict(task_routes or {}),
            beat_schedule=dict(beat_schedule or {}),
            **conf,
        )
        self._connect_signals()

    # -- the per-process runtime -------------------------------------------------

    def runtime(self) -> WorkerRuntime:
        """This process's runtime, built on first use.

        Lazy rather than on ``worker_process_init`` because the solo pool and eager
        mode never fire that signal, and the lazy path serves all three.
        """
        if self._runtime is None:
            self._ensure_tracing()
            self._runtime = self._runtime_factory(self.settings)
        return self._runtime

    def _close(self) -> None:
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None
        if self._traced:
            from py_common.telemetry import shutdown_tracing

            shutdown_tracing()
            self._traced = False

    def _ensure_tracing(self) -> None:
        otel = getattr(self.settings, "otel", None)
        if self._traced or otel is None or not otel.enabled:
            return
        self._traced = True
        from py_common.telemetry import setup_tracing

        setup_tracing(
            service_name=f"{self._service_name()}",
            otlp_endpoint=otel.exporter_otlp_endpoint,
            insecure=otel.exporter_otlp_insecure,
            sampler_arg=otel.traces_sampler_arg,
            environment=self._environment(),
            redact_query_params=otel.redact_query_params,
        )

    def _service_name(self) -> str:
        app = getattr(self.settings, "app_name", self.name)
        if self._beat:
            return self._beat_service_name or f"{app}-beat"
        return f"{app}-worker"

    def _environment(self) -> str:
        environment = getattr(self.settings, "environment", None)
        return str(getattr(environment, "value", environment or "dev"))

    # -- tasks -------------------------------------------------------------------

    def task(
        self,
        name: str,
        *,
        max_retries: int | None = None,
        backoff_seconds: int | None = None,
        lock: bool | float = False,
        **options: Any,
    ) -> Callable[[AsyncJob], Any]:
        """Register ``async def job(runtime, *args)`` as the Celery task ``name``.

        Retries on any exception with exponential back-off (capped at ten minutes,
        with jitter), up to ``max_retries`` (default ``WORKER__MAX_RETRIES``), then
        records a dead letter. ``lock=True`` -- or a TTL in seconds -- takes a
        non-blocking Redis lock named after the task, so a scheduled run and a manual
        one never overlap; the loser returns ``None`` without running.
        """
        worker = self.settings.worker
        retries = worker.max_retries if max_retries is None else max_retries
        backoff = worker.retry_backoff_seconds if backoff_seconds is None else backoff_seconds
        ttl = worker.lock_ttl_seconds if lock is True else float(lock)

        # Any, and called rather than used as `@decorator`: celery ships no types, and
        # mypy words (and, between versions, codes) the "untyped decorator" error
        # differently, so no single `type: ignore` is valid across the versions this
        # library supports.
        register: Any = self.celery.task

        def decorator(job: AsyncJob) -> Any:
            def run(*args: Any) -> Any:
                runtime = self.runtime()
                return runtime.run(self._execute(job, runtime, name, ttl if lock else None, args))

            task = register(
                name=name,
                autoretry_for=(Exception,),
                retry_backoff=backoff,
                retry_backoff_max=600,
                retry_jitter=True,
                max_retries=retries,
                **options,
            )(run)
            task.__doc__ = job.__doc__
            task.job = job  # for tests
            return task

        return decorator

    @staticmethod
    async def _execute(
        job: AsyncJob,
        runtime: WorkerRuntime,
        name: str,
        lock_ttl: float | None,
        args: tuple[Any, ...],
    ) -> Any:
        if lock_ttl is None:
            return await job(runtime, *args)
        async with scheduled_lock(runtime, name, ttl_seconds=lock_ttl) as held:
            return await job(runtime, *args) if held else None

    # -- signals -----------------------------------------------------------------

    def _connect_signals(self) -> None:
        # weak=False: these are bound methods, which a weak reference would drop at once.
        # dispatch_uid per app so building a second Worker (tests) does not stack handlers.
        def connect(signal: Any, handler: Callable[..., None], what: str) -> None:
            signal.connect(handler, weak=False, dispatch_uid=f"py_common.worker.{self.name}.{what}")

        connect(signals.setup_logging, self._on_setup_logging, "logging")
        connect(signals.worker_process_init, self._on_process_init, "process_init")
        connect(signals.beat_init, self._on_beat_init, "beat_init")
        connect(signals.worker_process_shutdown, self._on_process_shutdown, "process_shutdown")
        connect(signals.task_prerun, self._on_task_prerun, "prerun")
        connect(signals.task_postrun, self._on_task_postrun, "postrun")
        connect(signals.task_failure, self._on_task_failure, "failure")

    def _owns(self, task: Any) -> bool:
        return getattr(task, "app", None) is self.celery

    def _on_setup_logging(self, **_: Any) -> None:
        # Connecting to this signal is what stops Celery configuring logging itself.
        self._configure_logging()

    def _configure_logging(self) -> None:
        setup_logging(
            level=str(getattr(self.settings, "log_level", "INFO")),
            service_name=self._service_name(),
            environment=self._environment(),
            json_logs=self._json_logs,
        )

    def _on_process_init(self, **_: Any) -> None:
        self._ensure_tracing()

    def _on_beat_init(self, **_: Any) -> None:
        # Celery configures logging before it announces beat, with no hint that this
        # process is beat, so the logger is set up again now that the name is known.
        self._beat = True
        self._configure_logging()
        self._ensure_tracing()

    def _on_process_shutdown(self, **_: Any) -> None:
        self._close()

    def _on_task_prerun(
        self, sender: Any = None, task_id: str | None = None, task: Any = None, **_: Any
    ) -> None:
        if not self._owns(task):
            return
        retries = getattr(getattr(task, "request", None), "retries", 0)
        structlog.contextvars.bind_contextvars(
            task={"name": task.name, "id": task_id, "retries": retries}
        )

    def _on_task_postrun(self, sender: Any = None, task: Any = None, **_: Any) -> None:
        if self._owns(task):
            structlog.contextvars.unbind_contextvars("task")

    def _on_task_failure(
        self,
        sender: Any = None,
        args: Any = None,
        exception: BaseException | None = None,
        **_: Any,
    ) -> None:
        """A task that ran out of retries: keep what is needed to run it again."""
        if not self._owns(sender) or exception is None:
            return
        entry = dead_letter_entry(
            getattr(sender, "name", str(sender)), args, exception, time.time()
        )
        logger.error("job_failed", entry=entry)
        try:
            runtime = self.runtime()
            push: Any = runtime.redis.rpush(self.settings.worker.dead_letter_key, entry)
            runtime.run(push)
        except Exception:
            logger.exception("dead_letter_write_failed")
