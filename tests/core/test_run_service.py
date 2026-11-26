import runpy
from pathlib import Path
from unittest.mock import Mock

import pytest

import app.core.logging as service_logging
from app.core import settings
from app.core.settings import LogLevel


@pytest.mark.parametrize("module_name,worker", [("__mp_main__", True), ("imported_entry", False)])
def test_reload_worker_configures_logs_without_starting_another_server(
    monkeypatch, module_name, worker
):
    import uvicorn

    configure = Mock()
    run = Mock()
    monkeypatch.setattr(service_logging, "configure_logging", configure)
    monkeypatch.setattr(uvicorn, "run", run)
    runpy.run_path(str(Path(__file__).parents[2] / "app/__main__.py"), run_name=module_name)
    run.assert_not_called()
    if worker:
        configure.assert_called_once_with(settings.LOG_LEVEL, settings.LOG_FORMAT)
    else:
        configure.assert_not_called()


def test_uvicorn_receives_service_logging_for_spawned_workers(monkeypatch):
    import uvicorn

    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(service_logging, "configure_logging", Mock())
    monkeypatch.setattr(settings, "LOG_LEVEL", LogLevel.INFO)
    runpy.run_path(str(Path(__file__).parents[2] / "app/__main__.py"), run_name="__main__")
    log_config = run.call_args.kwargs["log_config"]
    assert log_config["root"]["level"] == "INFO"
    assert log_config["handlers"]["service"]["filters"] == ["service"]
