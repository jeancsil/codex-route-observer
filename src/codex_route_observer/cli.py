import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from .app import create_app
from .settings import Settings


def codex_command(args, port, auth):
    prefix = f"http://127.0.0.1:{port}"
    base = prefix + ("/backend-api/codex" if auth == "chatgpt" else "/v1")
    provider = "route_observer"
    command = ["codex", "-c", f'model_provider="{provider}"']
    values = {
        "name": "Codex Route Observer",
        "base_url": base,
        "wire_api": "responses",
        "supports_websockets": True,
        "requires_openai_auth": auth == "chatgpt",
    }
    if auth == "api":
        values["env_key"] = "OPENAI_API_KEY"
    for key, value in values.items():
        command += ["-c", f"model_providers.{provider}.{key}={json.dumps(value)}"]
    return command + args


def main():
    parser = argparse.ArgumentParser(
        description="Observe Codex model requests with Jev recommendations"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Run the local proxy and dashboard")
    serve.add_argument("--port", type=int, default=int(os.getenv("OBSERVER_PORT", "9393")))
    serve.add_argument("--policy", type=Path)
    serve.add_argument("--db", type=Path)
    codex = sub.add_parser("codex", help="Launch Codex through the running proxy")
    codex.add_argument("--port", type=int, default=int(os.getenv("OBSERVER_PORT", "9393")))
    codex.add_argument("--auth", choices=["chatgpt", "api"], default="chatgpt")
    codex.add_argument("args", nargs=argparse.REMAINDER, help="Codex arguments after --")
    install_parser = sub.add_parser(
        "install", help="Start automatically at macOS login and make plain codex use the observer"
    )
    install_parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if args.command == "install":
        from .service import install

        try:
            install(Path.cwd(), args.port)
        except (ValueError, RuntimeError) as exc:
            parser.error(str(exc))
    elif args.command == "serve":
        settings = Settings()
        if args.policy:
            settings.policy_path = args.policy
        if args.db:
            settings.db_path = args.db
        print(f"Dashboard: http://127.0.0.1:{args.port}", flush=True)
        print("Mode: observe; the requested model is preserved.", flush=True)
        if not settings.api_key:
            print(
                "TYPESAFE_API_KEY is absent; requests will be recorded without recommendations.",
                flush=True,
            )
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, access_log=False)
    else:
        binary = shutil.which("codex")
        local_binary = Path.home() / ".local" / "bin" / "codex"
        if not binary and local_binary.is_file():
            binary = str(local_binary)
        if not binary:
            parser.error("codex is not installed or not on PATH")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{args.port}/health", timeout=2
            ) as response:
                health = json.load(response)
            if health.get("service") != "codex-route-observer":
                raise ValueError("This port belongs to another service")
        except (OSError, ValueError, urllib.error.URLError):
            parser.error("Start the observer first: uv run codex-route-observer serve")
        forwarded = args.args[1:] if args.args[:1] == ["--"] else args.args
        command = codex_command(forwarded, args.port, args.auth)
        command[0] = binary
        # No persistent Codex config edits, credential reads, or model overrides.
        sys.stdout.flush()
        os.execvp(command[0], command)
