"""Profiler helper: disabled by default, production dashboard guard."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi import FastAPI

from py_common.config import ProfilerSettings
from py_common.telemetry.profiler import enable_profiler


def test_profiler_noop_when_disabled() -> None:
    app = FastAPI()
    assert enable_profiler(app, ProfilerSettings(enabled=False)) is False


def test_profiler_noop_when_package_missing() -> None:
    app = FastAPI()
    with patch(
        "py_common.telemetry.profiler.enable_profiler.__module__",
        create=True,
    ):
        # Patch the import inside enable_profiler
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "fastapi_profiler":
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        with patch("builtins.__import__", side_effect=fake_import):
            assert enable_profiler(app, ProfilerSettings(enabled=True)) is False


def test_profiler_blocks_dashboard_in_production() -> None:
    app = FastAPI()
    mock_cls = MagicMock(name="PyInstrumentProfilerMiddleware")

    with patch.dict(
        "sys.modules",
        {"fastapi_profiler": MagicMock(PyInstrumentProfilerMiddleware=mock_cls)},
    ):
        result = enable_profiler(
            app,
            ProfilerSettings(
                enabled=True,
                enable_dashboard=True,
                allow_dashboard_in_production=False,
            ),
            environment="production",
        )

    assert result is True
    # add_middleware(cls, **kwargs) — dashboard must be forced off
    assert app.user_middleware, "middleware should have been added"
    kwargs = app.user_middleware[0].kwargs
    assert kwargs.get("enable_dashboard") is False


def test_profiler_settings_defaults_are_safe() -> None:
    s = ProfilerSettings()
    assert s.enabled is False
    assert s.enable_dashboard is False
    assert s.sample_rate == 0.1
    assert s.always_profile_errors is True
