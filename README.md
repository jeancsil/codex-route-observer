# Codex Route Observer

A local proxy for **Codex CLI** that records the requested model, forwards the
request unchanged, and asks **Jev** which model would suit the next step. A web UI
compares the requested, upstream-reported, and recommended models.

The first version observes only. It does not compress prompts, change models,
alter reasoning settings, or run the same request against multiple models.
Recommendations are prospective policy judgments, not measured proof of how an
alternative model would have performed.

## Automatic setup on macOS

The observer can start at login, restart if it exits, and become the default
connection for **plain `codex` in every project**. Set it up once:

```sh
cd ~/code/codex-route-observer
uv sync --group dev
# Reuses your existing exported TYPESAFE_API_KEY.
uv run codex-route-observer install
```

Then start Codex normally from any project:

```sh
cd ~/code/my-project
codex
```

The dashboard is always at **http://127.0.0.1:9393**. Existing Codex sessions need
one restart; their connection settings cannot be changed retroactively. The page
polls local records every three seconds while visible and Live updates is enabled.
Tool commands by themselves do not create model requests.

The installer creates the per-user LaunchAgent
`~/Library/LaunchAgents/ai.codex-route-observer.plist` and changes only the default
provider, its managed definition, and the built-in OpenAI base URL in
`~/.codex/config.toml`. The built-in URL override also captures resumed threads
whose saved provider is `openai`, rather than only new `route_observer` threads.
Your selected model,
reasoning effort, profiles, and unrelated settings are preserved. A dated backup
of the previous Codex config is saved alongside it. ChatGPT sign-in is reused;
there is no API-key billing switch for Codex.

For login startup, the existing Jev key and service settings are stored in
`~/Library/Application Support/codex-route-observer/service.json`, readable only
by your user (mode 0600). They are outside the repository and never printed or
included in the LaunchAgent plist. Logs are in
`~/Library/Logs/codex-route-observer/server.log`.

Reinstalling preserves the saved Jev key, endpoint, model, timeout, concurrency,
and port. Explicit environment variables override those saved values; `install
--port` takes precedence over `OBSERVER_PORT`. If replacement startup or a config
write fails, the installer restores the previous files and running service. Any
failure to complete recovery is reported explicitly.

Restart the installed service after changing Python code or policy:

```sh
launchctl kickstart -k gui/$(id -u)/ai.codex-route-observer
```

For a one-off direct ChatGPT connection, use
`codex -c 'model_provider="openai"' -c 'openai_base_url="https://chatgpt.com/backend-api/codex"'`.
To stop using the observer by default, restore the root `model_provider` setting
to `"openai"` and remove the observer's root `openai_base_url` override in
`~/.codex/config.toml`, then stop the service with
`launchctl bootout gui/$(id -u)/ai.codex-route-observer`. This leaves recorded data
and the config backup intact.

If a shared Codex background server is retaining older settings, exit its TUI and
resume once with `codex --no-daemon resume <SESSION_ID>` to use a fresh local
runtime. A resumed thread keeps its original conversation history.

## Manual launch (alternative)

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
- The theme toggle sits below the workspace label in the bottom-left sidebar.
  It follows the system theme initially and remembers an explicit choice.

### Jev usage

The **Jev usage** link jumps to a dedicated section on the same page. It records
each actual Jev HTTP attempt, its API time and queue delay, status, question count,
request/response sizes, reported input/output tokens, and calculated USD cost.
Filter by status or period and expand a row to read the recorded response JSON.
These views use only local SQLite data; inspecting them never invokes Jev.

Cost is calculated exactly from API-reported input tokens at the documented direct
TypeSafe list price: **$0.042 per million input tokens; output free**, verified
2026-10-18 for `jev-1.13.0` and `jev-1.14.0` at
[TypeSafe Models](https://docs.typesafe.ai/models). Each priced call stores the
returned version, integer nanodollar cost, applied rate, source, and verification
date. Costs are returned as decimal strings with nine places, without intermediate
rounding or floating-point summation. Account credits, discounts, and tax are not
reported by this endpoint and are excluded: this is precise list-price accounting,
not an invoice reconciliation.

Unknown versions, non-direct gateways, missing usage, and network failures have
**unavailable** costs, not a fabricated zero. Subtotals show their priced-call
coverage. An invalid verdict can still have reported/billable usage, so its cost
is preserved even when classification fails. Calls predating this instrumentation
have no saved usage or response; their costs cannot be recovered, and the page
explicitly reports that historical gap. No requests are replayed to fill it.

Response bodies are fetched from the local database only when expanded. They are
bounded to 65,536 characters, redact recognizable credential strings, and render
as text. Authorization headers and classifier input bodies are never persisted.

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
The latest 1,200-character user request excerpt, observation metadata, and bounded
Jev response bodies are persisted to SQLite. Full Codex request/response bodies,
Jev input bodies, and authentication headers are not stored. Prompt/context excerpts are sent to your configured TypeSafe endpoint
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
