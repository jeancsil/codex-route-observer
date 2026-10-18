# Codex Route Observer

A local proxy for **Codex CLI** that records the requested model, forwards the
request unchanged, and asks **Jev** which model would suit the next step. A web UI
compares the requested, upstream-reported, and recommended models.

The first version observes only. It does not compress prompts, change models,
alter reasoning settings, or run the same request against multiple models.
Recommendations are prospective policy judgments, not measured proof of how an
alternative model would have performed.

## Run

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and an installed Codex CLI.

```sh
cd ~/code/codex-route-observer
uv sync --group dev

# Reuse the TYPESAFE_API_KEY already exported in your shell.
uv run codex-route-observer serve
```

Open **http://127.0.0.1:9393**. In another terminal:

```sh
cd ~/code/codex-route-observer
uv run codex-route-observer codex --
# All arguments after -- go to Codex. Example:
uv run codex-route-observer codex -- --cd ~/code/my-project
```

The launcher uses your existing ChatGPT login and the model already configured in
Codex. It passes a temporary custom-provider override on the command line; it does
not edit `~/.codex/config.toml` or read `auth.json`. An already-running Codex session
is unaffected. Start a new session with the launcher to observe it.

For API-key authentication instead:

```sh
# OPENAI_API_KEY must be exported for this mode.
uv run codex-route-observer codex --auth api --
```

The ChatGPT mode forwards `/backend-api/codex/*` to
`https://chatgpt.com/backend-api/codex/*`; API mode forwards `/v1/*` to
`https://api.openai.com/v1/*`. Both SSE and Responses WebSocket transport are
supported. Models, compaction, tool-call items, continuation IDs, authorization,
and account headers are relayed. Credentials are not persisted in the observer.
Other Codex network traffic (plugins, MCP, sign-in, etc.) is outside this proxy.

## Explore

- Filter requests by sent/recommended model, task, mismatch, session, confidence,
  classification status, transport, and date/time.
- See a sent-to-recommended model comparison, request duration, reasoning effort,
  token usage when reported, classifier latency, and model probabilities.
- Open a row for the prompt excerpt and policy rationale. Export the visible page
  as CSV. Filters are preserved in the URL; live updates can be paused.
- Missing, low-confidence, interrupted, and failed classifications remain distinct.
  A recommendation mismatch does not mean the actual model failed.

Jev runs on a bounded queue with separate HTTP connections. Its classification
never gates forwarding. The proxy still necessarily adds a local network hop and
observation overhead. Queue saturation or classifier failure skips/fails a
classification rather than stopping the Codex request. There are no automatic
upstream retries in the proxy, to avoid duplicating model requests.

## Policy and configuration

Edit `policy.toml` and restart. The initial balanced policy uses three candidates
from the installed Codex catalog at creation time: `gpt-6-luna`, `gpt-6.1-sol`, and
`gpt-6-astra`. Replace them if your account's catalog differs. Model IDs and the
rubrics are configuration, not hidden ranking assumptions. There are no invented
price/savings calculations.

Jev receives three independent Choice questions in one call: model, task category,
and reasoning difficulty. Its model choice is not shown which model Codex selected.
The displayed rationale combines task/complexity labels with the selected model's
policy description; it is not a generated explanation of Jev's internal reasoning.

The classifier sees a bounded excerpt: latest user request, recent messages/tool
results, available instructions, and flags for tools, images, and continuation.
Images themselves are not sent to Jev. When a follow-up includes only tool results,
the observer reuses the most recent user request for that session from a bounded
in-memory cache. If no user request is available, it skips classification instead
of inventing one. Continuations using a different connection need stable session
headers to inherit context. Truncated/missing context is visible in request details.

| Environment variable | Default |
|---|---|
| `TYPESAFE_API_KEY` | Existing exported key; absent = observation without recommendations |
| `TYPESAFE_API_BASE` | `https://api.typesafe.ai` |
| `TYPESAFE_MODEL` | `jev-latest` |
| `OBSERVER_PORT` | `9393` |
| `OBSERVER_DB` | `data/observer.sqlite3` |
| `OBSERVER_POLICY` | `policy.toml` |
| `OBSERVER_JEV_TIMEOUT` | `10` seconds |
| `OBSERVER_CLASSIFICATION_CONCURRENCY` | `2` |

`.env.example` is a reference; environment files are not loaded automatically.
Only the latest 1,200-character user request excerpt and observation metadata are
persisted to SQLite. Full request/response bodies and authentication headers are
not stored. Prompt/context excerpts are sent to your configured TypeSafe endpoint
for classification. The service binds to `127.0.0.1`; the dashboard is intended for
local use. Data lives in the ignored `data/` directory until you remove it.

## Development

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Tests use fake upstreams and fake Jev responses; no keys or live model calls are
required. They exercise unchanged forwarding, streamed terminal events, metadata,
classifier failure isolation, compression decoding, session context, policy
decisions, filtering, and launcher configuration.

The initial implementation was also checked locally with Codex CLI 0.163.0 and
ChatGPT sign-in: a real WebSocket response, a read-only `pwd` tool-call/follow-up
round trip, and live Jev classification using the existing environment key all
completed successfully. Dashboard rendering, filter/reset behavior, request and
policy dialogs, prompt HTML escaping, URL filters, and pause controls were checked
with a DOM test harness. Browser screenshot verification was unavailable in the
development session.

## References

- [Codex custom model providers](https://developers.openai.com/codex/config-advanced)
- [ChatGPT authentication through a gateway](https://learn.chatgpt.com/docs/enterprise/sign-in-with-chatgpt-through-a-gateway)
- [Codex gateway compatibility](https://learn.chatgpt.com/docs/enterprise/gateway-compatibility)
- [TypeSafe evaluation API](https://docs.typesafe.ai/api)
- [TypeSafe Choice](https://docs.typesafe.ai/primitives/choice)
