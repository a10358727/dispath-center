import sys
from types import ModuleType

import pytest

from dispatch_center import cli


@pytest.mark.parametrize(
    ("entry_point", "program"),
    [
        (cli.main, "dispatch"),
        (cli.api, "dispatch-api"),
        (cli.scheduler, "dispatch-scheduler"),
        (cli.worker, "dispatch-worker"),
    ],
)
def test_console_help_is_side_effect_free(entry_point, program, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_serve", lambda role: pytest.fail(f"started {role}"))

    with pytest.raises(SystemExit) as exc_info:
        entry_point(["--help"])

    assert exc_info.value.code == 0
    assert f"usage: {program}" in capsys.readouterr().out


def test_dispatch_without_a_command_prints_help_without_starting(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_serve", lambda role: pytest.fail(f"started {role}"))

    assert cli.main([]) == 0

    assert "usage: dispatch" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["api", "scheduler", "worker"])
def test_dispatch_selects_an_explicit_process_role(command, monkeypatch):
    roles: list[str] = []
    monkeypatch.setattr(cli, "_serve", lambda role: roles.append(role) or 0)

    assert cli.main([command]) == 0
    assert roles == [command]


def test_role_launcher_sets_process_role_temporarily_before_running_application(
    monkeypatch,
):
    observations: list[str | None] = []
    fake_main = ModuleType("app.main")
    monkeypatch.setenv("PROCESS_ROLE", "all")

    def run() -> None:
        observations.append(cli.os.environ.get("PROCESS_ROLE"))

    fake_main.run = run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.main", fake_main)

    assert cli._serve("api") == 0
    assert observations == ["api"]
    assert cli.os.environ["PROCESS_ROLE"] == "all"


def test_role_launcher_removes_role_it_introduced(monkeypatch):
    fake_main = ModuleType("app.main")
    fake_main.run = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.main", fake_main)
    monkeypatch.delenv("PROCESS_ROLE", raising=False)

    assert cli._serve("scheduler") == 0
    assert "PROCESS_ROLE" not in cli.os.environ


def test_worker_launcher_uses_non_http_worker_entrypoint(monkeypatch):
    fake_main = ModuleType("app.main")
    calls: list[str] = []
    fake_main.run = lambda: calls.append("http")  # type: ignore[attr-defined]
    fake_main.run_worker = lambda: calls.append("worker")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.main", fake_main)

    assert cli._serve("worker") == 0
    assert calls == ["worker"]


def test_module_launcher_passes_loaded_app_to_uvicorn(monkeypatch):
    import app.main as main_module

    captured: dict[str, object] = {}
    fake_uvicorn = ModuleType("uvicorn")

    class _Config:
        api_host = "127.0.0.1"
        api_port = 8000

    def fake_run(application, **kwargs):
        captured["application"] = application
        captured.update(kwargs)

    fake_uvicorn.run = fake_run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(main_module, "load_app_config", lambda: _Config())

    main_module.run()

    assert captured["application"] is main_module.app
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8000
    assert captured["reload"] is False
