"""Per-user macOS startup and persistent Codex provider configuration."""

import copy
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from .settings import Settings

LABEL = "ai.codex-route-observer"
BEGIN = "# BEGIN codex-route-observer managed provider"
END = "# END codex-route-observer managed provider"


def service_config_path():
    return Path.home() / "Library/Application Support/codex-route-observer/service.json"


def configure_codex(original: str, port: int) -> str:
    """Preserve unrelated settings and comments, validating the resulting structure."""
    previous = tomllib.loads(original)
    if previous.get("model_provider", "openai") not in {"openai", "route_observer"}:
        raise ValueError("Automatic ChatGPT setup would replace your selected custom provider")
    provider = {
        "name": "Codex Route Observer",
        "base_url": f"http://127.0.0.1:{port}/backend-api/codex",
        "wire_api": "responses",
        "requires_openai_auth": True,
        "supports_websockets": True,
    }
    block = (
        BEGIN
        + "\n[model_providers.route_observer]\n"
        + "\n".join(f"{key} = {json.dumps(value)}" for key, value in provider.items())
        + "\n"
        + END
    )
    if BEGIN in original:
        updated, count = re.subn(
            re.escape(BEGIN) + r".*?" + re.escape(END), lambda _: block, original, flags=re.S
        )
        if count != 1:
            raise ValueError("Expected one managed provider block")
    elif "route_observer" in previous.get("model_providers", {}):
        if previous["model_providers"]["route_observer"] != provider:
            raise ValueError("An unrelated route_observer provider already exists")
        updated = original
    else:
        updated = original.rstrip() + "\n\n" + block + "\n"
    if "model_provider" in previous:
        # A root setting must occur before the first TOML table. Structural
        # validation below protects nested profiles or multiline values.
        first_table = re.search(r"(?m)^\s*\[", updated)
        boundary = first_table.start() if first_table else len(updated)
        head, count = re.subn(
            r"(?m)^\s*model_provider\s*=.*$",
            'model_provider = "route_observer"',
            updated[:boundary],
        )
        if count != 1:
            raise ValueError("Could not safely replace the root model_provider setting")
        updated = head + updated[boundary:]
    else:
        updated = 'model_provider = "route_observer"\n' + updated
    # Resumed threads may retain the built-in provider ID saved in their history.
    # Cover that provider as well as the default used by newly created threads.
    previous_url = previous.get("openai_base_url")
    managed_url = previous.get("model_providers", {}).get("route_observer", {}).get("base_url")
    if previous_url and previous_url not in {provider["base_url"], managed_url}:
        raise ValueError("An unrelated built-in OpenAI endpoint is already configured")
    if "openai_base_url" in previous:
        first_table = re.search(r"(?m)^\s*\[", updated)
        boundary = first_table.start() if first_table else len(updated)
        head, count = re.subn(
            r"(?m)^\s*openai_base_url\s*=.*$",
            "openai_base_url = " + json.dumps(provider["base_url"]),
            updated[:boundary],
        )
        if count != 1:
            raise ValueError("Could not safely update the root openai_base_url setting")
        updated = head + updated[boundary:]
    else:
        updated = "openai_base_url = " + json.dumps(provider["base_url"]) + "\n" + updated
    expected = copy.deepcopy(previous)
    expected["model_provider"] = "route_observer"
    expected["openai_base_url"] = provider["base_url"]
    expected.setdefault("model_providers", {})["route_observer"] = provider
    if tomllib.loads(updated) != expected:
        raise ValueError("Refusing a configuration change that alters unrelated settings")
    return updated


def launch_agent(project: Path, log_dir: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(project / ".venv/bin/python"),
            "-m",
            "codex_route_observer.service",
        ],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": 10,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(log_dir / "server.log"),
        "StandardErrorPath": str(log_dir / "server.log"),
    }


def private_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def health(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
        result = json.load(response)
    if result.get("service") != "codex-route-observer":
        raise ValueError("Port belongs to a different service")
    return result


def wait_for_service(port):
    for _ in range(50):
        try:
            result = health(port)
            if not result.get("ok") or not result.get("classification_enabled"):
                raise ValueError("Observer started without a healthy classifier configuration")
            return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise RuntimeError("Observer did not become healthy")


def read_optional(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def restore_file(path, previous):
    if read_optional(path) == previous:
        return
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        private_write(path, previous)


def saved_settings(saved):
    """Environment overrides > persisted service values > first-install defaults."""
    defaults = Settings()
    fields = {
        "api_key": "TYPESAFE_API_KEY",
        "typesafe_base": "TYPESAFE_API_BASE",
        "typesafe_model": "TYPESAFE_MODEL",
        "jev_timeout": "OBSERVER_JEV_TIMEOUT",
        "concurrency": "OBSERVER_CLASSIFICATION_CONCURRENCY",
    }
    values = {
        field: getattr(defaults, field)
        if variable in os.environ
        else saved.get(field, getattr(defaults, field))
        for field, variable in fields.items()
    }
    if not values["api_key"]:
        raise ValueError("Export your existing TYPESAFE_API_KEY before installing")
    # Validate saved numeric values as well as fresh environment overrides before
    # writing files or changing launchd registration.
    values["jev_timeout"] = float(values["jev_timeout"])
    values["concurrency"] = int(values["concurrency"])
    Settings(**values)
    return values


def rollback_service(
    domain,
    target,
    agent,
    saved_path,
    snapshots,
    registered,
    stop_attempted,
    replacement_attempted,
    previous_port,
):
    errors = []
    can_restart = True
    if replacement_attempted:
        try:
            stopped = subprocess.run(["launchctl", "bootout", target], capture_output=True)
            if (
                stopped.returncode
                and subprocess.run(["launchctl", "print", target], capture_output=True).returncode
                == 0
            ):
                errors.append("replacement service could not be stopped")
                can_restart = False
        except Exception:
            errors.append("replacement service cleanup failed")
            can_restart = False
    for path, previous in snapshots.items():
        try:
            restore_file(path, previous)
        except Exception:
            errors.append(f"could not restore {path.name}")
            if path in {agent, saved_path}:
                can_restart = False
    if registered and stop_attempted and can_restart:
        try:
            # A failed bootout may have left the original service registered.
            # Avoid replacing that still-running original unnecessarily.
            if subprocess.run(["launchctl", "print", target], capture_output=True).returncode != 0:
                subprocess.run(
                    ["launchctl", "bootstrap", domain, str(agent)], check=True, capture_output=True
                )
            wait_for_service(previous_port)
        except Exception:
            errors.append("previous service could not be restarted and verified")
    return errors


def install(project: Path, port: int | None = None):
    if sys.platform != "darwin":
        raise ValueError("Automatic startup currently supports macOS only")
    project = project.resolve()
    if not (project / "policy.toml").is_file() or not (project / ".venv/bin/python").is_file():
        raise ValueError("Run install from the observer repository after uv sync")
    saved_path = service_config_path()
    previous_saved = read_optional(saved_path)
    saved = json.loads(previous_saved) if previous_saved is not None else {}
    port = int(port if port is not None else os.getenv("OBSERVER_PORT", saved.get("port", 9393)))
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    config_file = Path.home() / ".codex/config.toml"
    previous_config = read_optional(config_file)
    original = previous_config.decode() if previous_config is not None else ""
    updated = configure_codex(original, port)
    values = saved_settings(saved)
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"
    registered = subprocess.run(["launchctl", "print", target], capture_output=True).returncode == 0
    agent = Path.home() / f"Library/LaunchAgents/{LABEL}.plist"
    previous_agent = read_optional(agent)
    if registered and (previous_saved is None or previous_agent is None):
        raise ValueError(
            "Cannot safely replace a registered observer without its saved service files"
        )
    if not registered:
        try:
            health(port)
        except (OSError, urllib.error.URLError):
            pass
        else:
            raise ValueError(
                "Stop the manually started observer before installing the automatic service"
            )
    log_dir = Path.home() / "Library/Logs/codex-route-observer"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = config_file.with_name(
        "config.toml.before-route-observer-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    )
    private_write(backup, original.encode())
    replacement = {**saved, **values, "port": port, "project": str(project)}
    # Serialize candidates and snapshot originals before any service mutation.
    new_saved = json.dumps(replacement).encode()
    new_agent = plistlib.dumps(launch_agent(project, log_dir))
    snapshots = {saved_path: previous_saved, agent: previous_agent}
    stop_attempted = replacement_attempted = False
    try:
        private_write(saved_path, new_saved)
        private_write(agent, new_agent)
        if registered:
            stop_attempted = True
            subprocess.run(["launchctl", "bootout", target], check=True, capture_output=True)
        replacement_attempted = True
        subprocess.run(
            ["launchctl", "bootstrap", domain, str(agent)], check=True, capture_output=True
        )
        wait_for_service(port)
        # Commit Codex routing only after the independent replacement is healthy.
        snapshots[config_file] = previous_config
        private_write(config_file, updated.encode())
    except BaseException as exc:
        errors = rollback_service(
            domain,
            target,
            agent,
            saved_path,
            snapshots,
            registered,
            stop_attempted,
            replacement_attempted,
            saved.get("port", port),
        )
        if errors:
            raise RuntimeError(
                "Installation failed; rollback incomplete: " + "; ".join(errors)
            ) from exc
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise RuntimeError(
            "Installation failed; previous files and service state restored"
        ) from exc
    print(f"Observer starts at login and restarts automatically: {target}")
    print(f"Codex default provider: route_observer → http://127.0.0.1:{port}/backend-api/codex")
    print("Resumed built-in OpenAI threads use the same observer endpoint.")
    print(f"Previous Codex config backed up: {backup}")
    print("Existing Jev key saved in a private, user-only service configuration; never printed.")
    print("Restart existing Codex sessions once. Future sessions use plain codex.")


def main():
    import uvicorn

    from .app import create_app

    config = json.loads(service_config_path().read_text())
    project = Path(config["project"])
    settings = Settings(
        api_key=config["api_key"],
        db_path=project / "data/observer.sqlite3",
        policy_path=project / "policy.toml",
        typesafe_base=config["typesafe_base"],
        typesafe_model=config["typesafe_model"],
        jev_timeout=config["jev_timeout"],
        concurrency=config["concurrency"],
    )
    uvicorn.run(create_app(settings), host="127.0.0.1", port=config["port"], access_log=False)


if __name__ == "__main__":
    main()
