import json
import plistlib
import subprocess
import tomllib
import urllib.error
from types import SimpleNamespace

import pytest

from codex_route_observer import service
from codex_route_observer.service import BEGIN, configure_codex, launch_agent, private_write


@pytest.mark.parametrize(
    "text",
    [
        'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "high"\n# keep my comment\n[projects."/code/example"]\ntrust_level="trusted"\n',
        'model_provider = "openai"\nmodel="gpt-6-sol"\n[profiles.other]\nmodel_provider="another"\n',
        "",
    ],
)
def test_persistent_provider_preserves_models_profiles_and_other_settings(text):
    original = tomllib.loads(text)
    updated = configure_codex(text, 9393)
    data = tomllib.loads(updated)
    assert data["model_provider"] == "route_observer"
    assert data["openai_base_url"] == "http://127.0.0.1:9393/backend-api/codex"
    assert data["model_providers"]["route_observer"]["requires_openai_auth"]
    for key in ("model", "model_reasoning_effort", "profiles", "projects"):
        assert data.get(key) == original.get(key)
    if "# keep my comment" in text:
        assert "# keep my comment" in updated
    again = configure_codex(updated, 9494)
    assert again.count(BEGIN) == 1
    assert (
        tomllib.loads(again)["model_providers"]["route_observer"]["base_url"]
        == "http://127.0.0.1:9494/backend-api/codex"
    )
    assert tomllib.loads(again)["openai_base_url"] == "http://127.0.0.1:9494/backend-api/codex"


def test_reinstall_does_not_override_custom_provider_even_with_managed_block():
    installed = configure_codex('model="gpt-6.1-sol"\n', 9393)
    changed = installed.replace(
        'model_provider = "route_observer"', 'model_provider = "custom_gateway"'
    )
    with pytest.raises(ValueError, match="custom provider"):
        configure_codex(changed, 9393)


def test_unrelated_builtin_endpoint_is_preserved():
    with pytest.raises(ValueError, match="unrelated built-in"):
        configure_codex('openai_base_url="https://company.example/v1"\n', 9393)


def test_conflicting_unmanaged_provider_is_not_overwritten():
    with pytest.raises(ValueError):
        configure_codex(
            '[model_providers.route_observer]\nbase_url="https://another.example"', 9393
        )


def test_service_plist_contains_no_credentials_and_private_files_are_user_only(tmp_path):
    agent = launch_agent(tmp_path, tmp_path / "logs")
    encoded = plistlib.dumps(agent)
    decoded = plistlib.loads(encoded)
    assert decoded["RunAtLoad"] and decoded["KeepAlive"]
    assert decoded["ProgramArguments"][0] == str(tmp_path / ".venv/bin/python")
    assert "api_key" not in encoded.decode() and "TYPESAFE_API_KEY" not in encoded.decode()
    path = tmp_path / "private/service.json"
    private_write(path, json.dumps({"api_key": "synthetic-key"}).encode())
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["api_key"] == "synthetic-key"


@pytest.fixture
def installer(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (project / ".venv/bin").mkdir(parents=True)
    (project / ".venv/bin/python").touch()
    (project / "policy.toml").write_text('version="synthetic"\n')
    monkeypatch.setattr(service.Path, "home", lambda: home)
    monkeypatch.setattr(service, "sys", SimpleNamespace(platform="darwin"))
    for variable in (
        "TYPESAFE_API_KEY",
        "TYPESAFE_API_BASE",
        "TYPESAFE_MODEL",
        "OBSERVER_JEV_TIMEOUT",
        "OBSERVER_CLASSIFICATION_CONCURRENCY",
        "OBSERVER_PORT",
    ):
        monkeypatch.delenv(variable, raising=False)
    config = home / ".codex/config.toml"
    saved = service.service_config_path()
    agent = home / f"Library/LaunchAgents/{service.LABEL}.plist"
    old_values = {
        "api_key": "synthetic-old-key",
        "port": 9494,
        "project": str(project),
        "typesafe_base": "https://custom.example/api",
        "typesafe_model": "custom-jev",
        "jev_timeout": 42.5,
        "concurrency": 7,
        "future_option": "preserve-me",
    }
    private_write(config, configure_codex('model="gpt-6.1-sol"\n', 9494).encode())
    private_write(saved, json.dumps(old_values, indent=2).encode())
    old_agent = launch_agent(project, home / "logs")
    old_agent["ThrottleInterval"] = 21
    private_write(agent, plistlib.dumps(old_agent))
    state = SimpleNamespace(
        home=home,
        project=project,
        config=config,
        saved=saved,
        agent=agent,
        old_values=old_values,
        registered=True,
        fault=None,
        triggered=False,
        commands=[],
        bootstrap_count=0,
        stop_count=0,
        wait_ports=[],
        originals={path: path.read_bytes() for path in (config, saved, agent)},
    )

    def run(command, check=False, **kwargs):
        assert command[0] == "launchctl", "Tests must never run real launchctl"
        operation = command[1]
        state.commands.append(operation)
        code = 0
        if operation == "print":
            code = 0 if state.registered else 5
        elif operation == "bootout":
            state.stop_count += 1
            if state.fault == "bootout" and not state.triggered:
                state.triggered = True
                code = 1  # Original service remains registered.
            elif state.fault == "interrupt" and not state.triggered:
                state.triggered = True
                state.registered = False
                raise KeyboardInterrupt()
            else:
                code = 0 if state.registered else 5
                state.registered = False
        elif operation == "bootstrap":
            state.bootstrap_count += 1
            if (
                state.fault in {"bootstrap", "partial-bootstrap", "recovery-fails"}
                and not state.triggered
            ):
                state.triggered = True
                state.registered = state.fault == "partial-bootstrap"
                code = 1
            elif state.fault == "recovery-fails":
                code = 1
            else:
                state.registered = True
        else:
            raise AssertionError(operation)
        if code and check:
            raise subprocess.CalledProcessError(code, command)
        return subprocess.CompletedProcess(command, code)

    def wait(port):
        state.wait_ports.append(port)
        if state.fault == "health" and not state.triggered:
            state.triggered = True
            raise RuntimeError("synthetic unhealthy replacement")
        assert state.registered

    real_write = service.private_write

    def write(path, content):
        if state.fault == "config-recovery-write" and path == config:
            if not state.triggered:
                state.triggered = True
                real_write(path, content)
            raise OSError("synthetic persistent Codex config write failure")
        paths = {"settings-write": saved, "agent-write": agent, "config-write": config}
        if state.fault in paths and path == paths[state.fault] and not state.triggered:
            state.triggered = True
            # Model a failure after publishing new contents, exercising recovery
            # even if the installer cannot assume a failed write changed nothing.
            real_write(path, content)
            raise OSError("synthetic file publication failure")
        real_write(path, content)

    def no_manual_service(port):
        raise urllib.error.URLError("synthetic: no manual listener")

    monkeypatch.setattr(service.subprocess, "run", run)
    monkeypatch.setattr(service, "wait_for_service", wait)
    monkeypatch.setattr(service, "health", no_manual_service)
    monkeypatch.setattr(service, "private_write", write)
    return state


@pytest.mark.parametrize(
    "fault",
    [
        "settings-write",
        "agent-write",
        "bootout",
        "bootstrap",
        "partial-bootstrap",
        "health",
        "config-write",
        "interrupt",
    ],
)
def test_failed_reinstall_restores_original_files_and_running_service(installer, fault):
    installer.fault = fault
    expected = KeyboardInterrupt if fault == "interrupt" else RuntimeError
    with pytest.raises(expected):
        service.install(installer.project, 9393)
    for path, original in installer.originals.items():
        assert path.read_bytes() == original, f"Failed to restore {path.name}"
    assert installer.registered, "The previous working service must be available again"
    if fault in {"settings-write", "agent-write"}:
        assert installer.stop_count == installer.bootstrap_count == 0
    else:
        assert installer.wait_ports[-1] == 9494, "Recovery must validate the OLD port"
    assert installer.saved.stat().st_mode & 0o777 == 0o600


def test_reinstall_preserves_saved_endpoint_model_tuning_and_port(installer):
    service.install(installer.project)
    assert json.loads(installer.saved.read_text()) == installer.old_values
    assert installer.wait_ports == [9494]
    assert installer.registered


def test_explicit_environment_overrides_saved_values(installer, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-new-key")
    monkeypatch.setenv("TYPESAFE_API_BASE", "https://new.example/v1")
    monkeypatch.setenv("TYPESAFE_MODEL", "new-model")
    monkeypatch.setenv("OBSERVER_JEV_TIMEOUT", "4.5")
    monkeypatch.setenv("OBSERVER_CLASSIFICATION_CONCURRENCY", "3")
    monkeypatch.setenv("OBSERVER_PORT", "9555")
    service.install(installer.project)
    values = json.loads(installer.saved.read_text())
    assert values == {
        **installer.old_values,
        "api_key": "synthetic-new-key",
        "typesafe_base": "https://new.example/v1",
        "typesafe_model": "new-model",
        "jev_timeout": 4.5,
        "concurrency": 3,
        "port": 9555,
    }
    assert installer.wait_ports == [9555]


def test_explicit_cli_port_takes_precedence_over_environment(installer, monkeypatch):
    monkeypatch.setenv("OBSERVER_PORT", "9555")
    service.install(installer.project, 9666)
    assert json.loads(installer.saved.read_text())["port"] == 9666


@pytest.mark.parametrize("fault", ["bootstrap", "health", "config-write"])
def test_failed_first_install_removes_new_service_files_and_restores_codex(
    installer, monkeypatch, fault
):
    installer.saved.unlink()
    installer.agent.unlink()
    installer.registered = False
    installer.config.write_text('model="gpt-6.1-sol"\n')
    original = installer.config.read_bytes()
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-first-key")
    installer.fault = fault
    with pytest.raises(RuntimeError):
        service.install(installer.project)
    assert not installer.saved.exists() and not installer.agent.exists()
    assert installer.config.read_bytes() == original
    assert not installer.registered


def test_incomplete_recovery_is_explicit_and_still_restores_files(installer):
    installer.fault = "recovery-fails"
    with pytest.raises(RuntimeError, match="rollback incomplete") as error:
        service.install(installer.project, 9393)
    assert "synthetic-old-key" not in str(error.value)
    for path, original in installer.originals.items():
        assert path.read_bytes() == original


def test_invalid_saved_settings_fail_before_service_or_file_mutations(installer):
    values = {**installer.old_values, "jev_timeout": -1}
    installer.saved.write_text(json.dumps(values))
    with pytest.raises(ValueError, match="positive"):
        service.install(installer.project)
    assert not installer.commands
    assert json.loads(installer.saved.read_text()) == values


def test_codex_config_recovery_failure_still_restarts_previous_service(installer):
    installer.fault = "config-recovery-write"
    with pytest.raises(RuntimeError, match="rollback incomplete: could not restore config.toml"):
        service.install(installer.project, 9393)
    assert installer.registered
    assert installer.wait_ports[-1] == 9494
    for path in (installer.saved, installer.agent):
        assert path.read_bytes() == installer.originals[path]


def test_atomic_write_failure_cleans_temporary_credentials(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    private_write(path, b"old")

    def failed_replace(source, destination):
        raise PermissionError("synthetic rename failure")

    monkeypatch.setattr(service.os, "replace", failed_replace)
    with pytest.raises(PermissionError):
        private_write(path, b"new synthetic credential")
    assert path.read_bytes() == b"old"
    assert not list(tmp_path.glob("settings.json.tmp-*"))
