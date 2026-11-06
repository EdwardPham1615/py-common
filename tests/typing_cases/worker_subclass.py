"""What a service with its own worker settings writes -- type-checked, never run.

``tests/test_worker_typing.py`` hands this file to ``mypy --strict`` and expects
nothing back. It is the shape thousand-lives has: a ``worker`` group whose class
extends ``WorkerSettings`` (a different default timezone), a ``WorkerRuntime`` subclass
that redeclares ``settings`` as the service's own class, and a ``Worker`` built from it.
Before ``WorkerHost`` was read-only each of those three lines needed an ignore comment.
"""

from __future__ import annotations

from py_common.config import BaseAppSettings, RedisSettings, WorkerSettings
from py_common.worker import Worker, WorkerRuntime


class AppWorkerSettings(WorkerSettings):
    timezone: str = "Asia/Ho_Chi_Minh"


class Settings(BaseAppSettings):
    worker: AppWorkerSettings = AppWorkerSettings()
    redis: RedisSettings = RedisSettings()
    queue_name: str = "default"


class Runtime(WorkerRuntime):
    settings: Settings

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.queue = settings.queue_name


worker = Worker("app", Settings(environment="dev"), runtime_factory=Runtime)
renamed = Worker("app", Settings(environment="dev").model_copy(update={"app_name": "x"}))
