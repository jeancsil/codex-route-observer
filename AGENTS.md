# Codex Route Observer

This is a separate project from llm-compressor. Preserve observation-only behavior:
the proxy must not change the requested model, reasoning settings, prompt, tool
items, or continuation IDs. Jev classification runs independently and must never
gate forwarding. Recommendations are policy judgments, not measured outcomes.

Use `uv sync --group dev`, `uv run pytest`, `uv run ruff check .`, and
`uv run ruff format --check .`. The app runs with
`uv run codex-route-observer serve`; launch Codex with
`uv run codex-route-observer codex --` in another terminal.

Keep model candidates and descriptions in `policy.toml`. Do not invent prices or
silently replace failed Jev classifications with heuristics. Display uncertainty,
unavailable context, and provider failures explicitly.

Never commit credentials, `.env`, database files, real prompt transcripts, or logs.
Do not read Codex auth.json: relay credentials provided by the client without
persisting them. Use synthetic prompts and fake upstreams in committed tests.
