# OpenCode Go Proxy

[![CI](https://github.com/kartikkabadi/opencode-go-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/kartikkabadi/opencode-go-proxy/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Dependencies: zstandard](https://img.shields.io/badge/dependencies-zstandard-blue.svg)](#)

Use your own [OpenCode Go](https://opencode.ai/docs/go) and
[OpenCode Zen](https://opencode.ai/zen) credentials in
[Codex](https://github.com/openai/codex) through a local protocol adapter.

Codex expects a Responses API (`/v1/responses`). OpenCode Go's documented
models span Responses, Chat Completions, and Anthropic Messages, while OpenCode
Zen also includes Gemini-family routes. This proxy selects the documented wire
protocol for each model in one local process:

```text
Codex app
    │
    │  POST /v1/responses (Responses API)
    ▼
opencode-go-proxy  ←── localhost:8787, one runtime dep (zstandard)
    │
    │  POST per family: chat/completions · responses · messages · models/<id>
    ▼
OpenCode Go / OpenCode Zen  ── 28 documented Go models · optional Zen models
```

## Project and policy boundary

OpenCode Go Proxy is an independent, single-user local adapter. It is not an
OpenAI or OpenCode product, and neither company sponsors or endorses it. It
does not provide shared accounts, pooled credentials, subscription resale, or
mechanisms to evade provider limits or safeguards.

The proxy uses Codex's documented `openai_base_url`, custom provider, and model
catalog configuration surfaces. When native OpenAI models are enabled, it
relays the user's existing Codex authorization only to the configured native
OpenAI endpoint over HTTPS; it never stores that authorization or uses it as
an OpenCode credential. OpenCode credentials are likewise never sent to the
native OpenAI endpoint.

Users are responsible for using their own accounts and following the current
[OpenAI Terms of Use](https://openai.com/terms),
[OpenAI Services Agreement](https://openai.com/policies/services-agreement/),
OpenCode terms, and any model-provider policies that apply to them. See
[Codex advanced configuration](https://developers.openai.com/codex/config-advanced)
and [SECURITY.md](SECURITY.md). This project documents technical safeguards;
it does not provide legal or policy certification.

## Why

OpenCode Go is a $10/month subscription for coding-agent access to a changing,
provider-curated model set. Codex speaks the Responses API, while Go models
currently require three different upstream protocols. This proxy provides that
adapter without replacing Codex's native OpenAI authorization.

## Quick start

```bash
# Configure Codex (writes one marker-delimited block to ~/.codex/config.toml)
uvx --from git+https://github.com/kartikkabadi/opencode-go-proxy \
  opencode-go-proxy config enable

# Run the loopback-only adapter
uvx --from git+https://github.com/kartikkabadi/opencode-go-proxy \
  opencode-go-proxy \
  --bind 127.0.0.1 \
  --port 8787

# Fully restart Codex, then select an explicit Go model
codex -m opencode-go/muse-spark-1.3-contributor
```

## Codex CLI and desktop setup

Install the official Codex CLI using OpenAI's current
[Codex CLI instructions](https://developers.openai.com/codex/cli). The
standalone macOS/Linux installer is:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

Then run `codex app` to open the installed Codex desktop app or start OpenAI's
official desktop installer. Run `opencode-go-proxy config enable` once, start
the loopback proxy, fully quit and reopen Codex, and select an
`opencode-go/<model-id>` entry.

The config command edits only its marker-delimited block in Codex's existing
configuration. It preserves unrelated settings, native ChatGPT authorization,
sessions, and desktop state. Run `opencode-go-proxy config disable` to remove
only that managed block.

## Available models

The checked-in catalog supports every model ID documented for OpenCode Go.
Use the explicit `opencode-go/<model-id>` form in Codex; it avoids ambiguity
when a Go model and a native OpenAI model have the same ID.

| Upstream protocol | Documented model IDs |
|-------------------|----------------------|
| Responses `/responses` | `grok-4.6`, `gpt-5.6-luna`, `muse-spark-1.3-contributor`, `muse-spark-1.2-contributor` |
| Chat Completions `/chat/completions` | `glm-5.3-flash`, `glm-5.3`, `glm-5.2`, `glm-5.1`, `kimi-k3`, `kimi-k2.7-code`, `kimi-k2.6`, `longcat-2.0`, `deepseek-v4.1-flash`, `deepseek-v4-pro`, `deepseek-v4-flash`, `deepseek-v4-flash-vision-exp`, `mimo-v2.5`, `mimo-v2.5-pro`, `hy4-preview`, `hy3` |
| Anthropic Messages `/messages` | `minimax-m3`, `minimax-m2.7`, `minimax-m2.5`, `qwen3.8-max`, `qwen3.8-flash`, `qwen3.7-max`, `qwen3.7-plus`, `qwen3.6-plus` |

The live `/models` response can change. Runtime refresh accepts only documented
or protocol-certified IDs, so an unknown ID is not guessed into the wrong
protocol. The checked-in seed keeps startup and model selection working
offline.

### HTTP API compatibility

The one listener accepts all three documented client formats, with or without
the `/v1` prefix:

| Client request | Supported Go models | Upstream behavior |
|----------------|---------------------|-------------------|
| `POST /v1/responses` | all 28 documented models | translated to each model's certified Responses, Chat Completions, or Messages family |
| `POST /v1/chat/completions` | the 16 Chat Completions models | relayed in Chat Completions format |
| `POST /v1/messages` | the 8 Anthropic Messages models | relayed in Messages format |

Streaming and non-streaming requests are supported on every row. The Responses
aliases `/responses/compact` and `/v1/responses/compact` are also available for
compaction. A model sent to an incompatible direct-format endpoint is rejected
before any upstream request rather than silently translated to a different
client response shape.

### Switching models

```bash
codex -m opencode-go/deepseek-v4-pro
codex -m opencode-go/glm-5.2
codex -m opencode-go/minimax-m3
```

### How models are routed

The proxy routes the exact model Codex sends:

1. `opencode-go/<id>` explicitly selects a known OpenCode Go catalog entry.
2. `zen/<id>` explicitly selects a known OpenCode Zen catalog entry.
3. A bare ID in the captured native catalog routes to the native OpenAI endpoint.
4. A known, non-colliding bare Go ID remains a compatibility alias; a known
   Zen-only bare ID routes to Zen.
5. An omitted model uses `deepseek-v4-flash`. An unknown or malformed model is
   rejected before any upstream request; the proxy never silently substitutes
   another model.

When images are present in a turn with tools, the proxy captions the latest image
(older ones are stubbed) and routes the main turn to your configured model. Image
turns without tools stay on the requested model when the catalog marks it
image-capable (`input_modalities` contains `image`); a text-only requested model
falls back to the image default. If the upstream still rejects the image payload
at runtime (400/404/415/422), the proxy captions the images and retries the
requested model once before failing the turn. The caption
engine auto-picks the cheapest image-capable model from the catalog (`input_modalities`
contains image) with `mimo-v2.5` as the fallback, or a local vision runtime (Ollama,
llama.cpp server, LM Studio) that answers a read-only probe. Captions are cached by
image bytes for an hour, sent with `detail: low` to cut upstream vision cost, and every
non-cached read is metered with `kind=vision`; the caption budget is 30s with no retries,
so a failed caption degrades to a placeholder instead of stalling the turn. Override the
engine with `CODEX_IMAGE_MODEL` or `OPENCODE_GO_PROXY_CAPTION_MODEL` (a model slug,
`local`, or default `auto` = catalog pick); configure a local runtime with
`OPENCODE_GO_PROXY_VISION_LOCAL_BASE_URL` / `OPENCODE_GO_PROXY_VISION_LOCAL_MODEL`.
Disable `detail: low` with `OPENCODE_GO_PROXY_CAPTION_DETAIL=none` (a rejected detail
value also falls back to a `sips`-downscaled image with no detail).

## OpenCode Zen

Since 0.4.0 the proxy also serves [OpenCode Zen](https://opencode.ai/zen), the
pay-as-you-go gateway with GPT, Claude, Gemini, Grok, DeepSeek, GLM, Kimi, and Qwen
models. Zen models are auto-discovered from `https://opencode.ai/zen/v1/models` (no
auth), merged with models.dev metadata, and appear in the catalog and `/v1/models`
as `zen/<id>` slugs (e.g. `zen/claude-sonnet-4-5`). Select one in the model
picker or run `codex -m zen/claude-sonnet-4-5`.

Requests route by model family, translated from the Responses API the proxy always
speaks to the surface the gateway expects:

| Model ids | Upstream surface | Auth header |
|-----------|------------------|-------------|
| `claude-*`, `qwen*` | `/zen/v1/messages` (Anthropic Messages) | `x-api-key` |
| `gemini-*` | `/zen/v1/models/<id>` (Gemini API) | `x-goog-api-key` |
| `gpt-*`, `grok-*` | `/zen/v1/responses` (Responses API, verbatim) | `Authorization: Bearer` |
| everything else | `/zen/v1/chat/completions` | `Authorization: Bearer` |

The proxy resolves the same key as Go — `$OPENCODE_GO_API_KEY` or the macOS keychain
entry `opencode-go-api-key` — and maps the auth header per family. Zen turns meter
with `provider="zen"`, so they never count against your Go quota.

Free models need no credits: `deepseek-v4-flash-free`, `mimo-v2.5-free`, and
`big-pickle` are always available.

The menu bar app shows Go usage polled from `https://opencode.ai/zen/go/v1/usage`
(60s cache) next to the fixed Go plan limits ($60/month, $30/week, $12 per rolling
5 hours), and a Zen rollup of today's turns/tokens and the last 7 days from the
local usage meter.

## API key

1. Sign in to [OpenCode Zen](https://opencode.ai/zen), subscribe to OpenCode
   Go, and copy your API key. OpenCode currently permits one Go subscriber per
   workspace.
2. Optionally verify the key in OpenCode itself: run `/connect`, select
   **OpenCode Go**, paste the key, then run `/models`.
3. Make the same user-owned key available to this proxy using one of the
   methods below.

The proxy resolves your OpenCode Go API key in this order:

1. `$OPENCODE_GO_API_KEY` environment variable
2. macOS keychain entry `opencode-go-api-key` (override with `CODEX_KEYCHAIN_SERVICE`; macOS only)

```bash
# Option 1: env var (works everywhere)
export OPENCODE_GO_API_KEY="your-key-here"

# Option 2: macOS keychain (macOS only)
security add-generic-password -a "$USER" -s opencode-go-api-key -w
```

The key is used only for Go or Zen upstream requests. It is never substituted
for Codex's native OpenAI authorization, and the separate remote caller token
is never forwarded upstream.

## Provider limits and data handling

- OpenCode Go's current limits are dollar-based: $12 per rolling five hours,
  $30 per week, and $60 per month. Model prices differ, so request counts vary.
- OpenCode may monitor traffic for abuse. The proxy identifies itself as
  `opencode-go-proxy/<version>` and sends a stable `x-opencode-session` value;
  override the user agent only when you can still identify the client
  truthfully with `OPENCODE_GO_PROXY_USER_AGENT`.
- Muse Spark Contributor models are region-limited and may use prompts or
  completions for training. They are not zero-data-retention models.
- DeepSeek V4 prices vary during documented peak periods. Vision model image
  inputs are billed from image dimensions as well as text.
- After Go limits are exhausted, OpenCode's console can optionally use a
  separate Zen balance. The proxy does not enable that setting or bypass a
  limit.
- Availability, pricing, retention, and limits are provider policies and may
  change. This adapter does not alter them; review the current
  [OpenCode Go documentation](https://opencode.ai/docs/go) before use.

## Recommended: lazycodex

[lazycodex](https://github.com/code-yeongyu/oh-my-openagent) is a Codex plugin that adds
multi-model orchestration, parallel background agents, and LSP/AST-aware tools. It pairs
naturally with this proxy — you get OpenCode Go's models as the backend and lazycodex's
agent harness on top.

```bash
npm install -g lazycodex-ai
```

See the [lazycodex docs](https://github.com/code-yeongyu/oh-my-openagent) for setup.

## Features

- OpenCode Zen support: auto-discovered `zen/<id>` catalog merged with models.dev metadata, per-family wire translation (Anthropic Messages / Gemini / Responses / chat-completions) with per-family auth header mapping, free models, and `provider="zen"` metering
- Responses `input` to chat `messages` translation
- `instructions` and `developer` roles mapped to system messages
- Function tool schema passthrough
- Custom/freeform tool adaptation (Codex `apply_patch` works)
- Reasoning content replay across tool-call turns
- Real-time SSE streaming (not synthesized)
- Cached image captioning when tools are present: cheapest catalog vision engine (or a probed local runtime) by default, `detail: low` input, 30s no-retry budget, MiMo V2.5 fallback, reads metered with `kind=vision`
- SSRF protection on image URLs (`data:image/` and `https://` only)
- Configurable body cap, peer-address guard, authenticated remote mode, keychain credential resolution
- Local health and model-list endpoints
- Prefix caching: byte-stable request prefixes plus `include_usage`, with per-model hit ratio on `/cache`
- Honest usage meter: append-only `usage-events.jsonl` in the state dir (truncated or empty responses never count as success)
- Upstream retry with bounded exponential backoff on transient failures (429/5xx/network/timeout)
- Ops CLI: `doctor` (reference-style checks with `--fix` for safe repairs), `smoke-test` (marker prompt through the local proxy), `support-bundle` (JSON schema v1, mode 0600), `install` (points at the macOS menu bar app; no launchd agent), `install-skills`, `refresh-runtime`, and `status`
- Spawned threads inherit the parent session's model (`create_thread`; `chatgptWorkCloud` targets are skipped)
- Correctness contract: empty upstream completions are retried once (a second empty stream answers an `empty_completion` error), zero-input-token reports are estimated for compaction (`OPENCODE_GO_PROXY_ESTIMATE_ZERO_INPUT=0` disables), and keepalive comments run until the stream truly ends without interleaving into data frames
- Auth transport guard (zero config): the default listener and accepted peers are loopback-only, forged `Host: localhost` does not admit a remote peer, browser-originated requests answer `403`, non-JSON POSTs answer `415`, and OPTIONS preflight stays blocked
- Family-aware Go routing for Responses, Chat Completions, and Anthropic
  Messages, including streaming and non-streaming requests with the
  family-specific authentication header
- Rate-limit harvesting (plan 011): upstream `x-ratelimit-*` and `anthropic-ratelimit-*` headers are parsed into per-provider quota snapshots, the latest snapshot per provider is kept, and `GET /quota` exposes `quota-state.json`
- Menu bar state contract (plan 013): `GET /state` returns one JSON document (status, port, upstream, latest quota snapshot, today's turns/tokens, last-7-day token bars, current model) computed from the meter file and quota state
- WebSocket upgrade requests answered with `426 Upgrade Required` (desktop app falls back to HTTP streaming)
- zstd-compressed request bodies decompressed (`Content-Encoding: zstd`; the desktop app sends them)
- Single-port guard in the menu bar app: refuses Start when 8787 is already owned

## Prefix caching

OpenCode Go bills cached reads at a fraction of normal input (`Cached Read` in the
pricing table), and its usage estimates assume most tokens per request are served from
cache. For that to happen the request prefix must be byte-stable: the same system
prompt, tools, and earlier conversation, in the same order, on every request.

The proxy is built for exactly that:

- The system prompt (`instructions` / `developer` roles) is always the first message.
- Conversation history is appended in order; translation is deterministic, so the
  serialized prefix is identical across requests in a session — and across sessions
  that share the same system prompt and tools.
- Streaming requests set `stream_options: { "include_usage": true }` so the upstream
  reports its cache accounting (`prompt_cache_hit_tokens` / `cached_tokens`) in the
  stream; without this most OpenAI-compatible endpoints omit usage entirely.
- Cache hits are surfaced to Codex in the standard Responses shape
  (`usage.input_tokens_details.cached_tokens`), so the app shows them in its token
  display.

### Checking the hit ratio

Every request with cache accounting is logged (look for `"cache": {...}` on
`response.converted` trace lines). The aggregate is also exposed on a metrics
endpoint:

```bash
curl http://127.0.0.1:8787/cache
```

```json
{
  "models": [
    {
      "model": "deepseek-v4-flash",
      "cache_hit_tokens": 100000,
      "cache_miss_tokens": 1000,
      "requests": 50,
      "hit_ratio": 0.990099
    }
  ],
  "totals": { "...": "..." }
}
```

Within a session, every request after the first shares the full previous prefix, so
cache-eligible requests typically run at 99%+ hit ratio; only the genuinely new delta
misses. The first request of a brand-new context always misses (nothing is cached yet).

### Checking quota

When the upstream answers 200 with rate-limit headers, the proxy records the latest
per-provider snapshot (`limit` / `remaining` / `resetAt` / `sampledAt`) to
`quota-state.json` in the state dir and exposes it as JSON:

```bash
curl http://127.0.0.1:8787/quota
```

```json
{
  "providers": {
    "openai": {
      "provider": "openai",
      "limit": 500,
      "remaining": 432,
      "resetAt": "2026-08-11T06:30:00.000Z",
      "sampledAt": "2026-08-11T06:00:00.000Z"
    }
  }
}
```

A headerless upstream leaves the state empty (`"providers": {}`); the proxy never
invents quota numbers.

### Checking state

`GET /state` (or `/v1/state`) composes the same local files into the single
contract the macOS menu bar reads. `quota` is the latest provider snapshot by
`sampledAt` (or `null`), `usage.last7d` is always seven entries (oldest first,
including today) so the UI renders a stable bar list, and `model` is the model of
the most recent meter event, falling back to the default:

```bash
curl http://127.0.0.1:8787/state
```

```json
{
  "status": "ok",
  "port": 8787,
  "upstream": "https://opencode.ai/zen/go/v1",
  "quota": {
    "provider": "openai",
    "remaining": 432,
    "limit": 500,
    "resetAt": "2026-08-11T06:30:00.000Z"
  },
  "usage": {
    "todayTurns": 12,
    "todayTokens": 3456,
    "last7d": [
      { "date": "2026-08-05", "tokens": 0 },
      { "date": "2026-08-06", "tokens": 1200 },
      { "date": "2026-08-07", "tokens": 900 },
      { "date": "2026-08-08", "tokens": 2100 },
      { "date": "2026-08-09", "tokens": 1800 },
      { "date": "2026-08-10", "tokens": 2700 },
      { "date": "2026-08-11", "tokens": 3456 }
    ],
    "go": {
      "rolling": { "...": "..." },
      "weekly": { "...": "..." },
      "monthly": { "...": "..." }
    },
    "goLimits": {
      "monthlyDollars": 60,
      "weeklyDollars": 30,
      "rolling5hDollars": 12
    },
    "zen": {
      "todayTurns": 3,
      "todayTokens": 812,
      "last7d": [0, 0, 0, 0, 1200, 400, 812]
    }
  },
  "model": "deepseek-v4-flash"
}
```

`usage.go` is the raw response of `GET https://opencode.ai/zen/go/v1/usage`
(`rolling` / `weekly` / `monthly` windows), TTL-cached for 60s and `null` when
the fetch fails. `usage.goLimits` holds the fixed Go dollar budgets the menu bar
renders next to it. `usage.zen` is the Zen-only rollup — turns and tokens for
today plus a seven-entry daily token list — from the local usage meter; the
legacy `todayTurns` / `todayTokens` / `last7d` keys stay as the all-provider
totals.

Missing or corrupt meter/quota files degrade to zeros or `null`; the endpoint
always returns this shape.

## Install

### From source (no package manager)

```bash
uvx --from git+https://github.com/kartikkabadi/opencode-go-proxy opencode-go-proxy --help
```

### From a development checkout

```bash
uv sync
uv run opencode-go-proxy --help
```

### macOS (menu bar)

The supported way to run the proxy on macOS is the menu bar app in
`macos/MenuBarApp`. Build it in Xcode (or `swift build`), launch it, and it
spawns the proxy itself and shows status, quota, and today's usage. There is
no launchd agent anymore: `contrib/launchd/` was removed in 0.3.0, and the
`install` ops command points at the menu bar app instead of a plist. The app
is the only supervisor: do not run another proxy service beside it.

## Updating

Releases are small patch bumps pushed to the same git URL, so updating means
re-installing from the newest tag. There is no PyPI and no separate package
channel.

### Menu bar (macOS)

The menu bar app checks the proxy version on start and on demand. When a
newer release exists it shows an "Update Available" row: click it, confirm,
and the app re-installs the proxy from the new tag and restarts it. Use
"Check for Updates" to force a fresh check. The menu bar app binary itself
is a manual rebuild; only the proxy it runs is auto-updated.

### CLI (uv tool install)

If you installed the proxy as a tool, check and apply updates with the CLI:

```bash
opencode-go-proxy update         # check for a newer release
opencode-go-proxy update --apply # install the newer release
```

`update` prints the installed version and the latest release (exit code 3
when an update is available). `--apply` re-installs from the new tag with
`uv tool install --force`; when the proxy was not installed as a tool it
prints the exact one-liner to run instead.

### Linux (systemd user service)

Install the checked-in user service, then start its single proxy process:

```bash
mkdir -p ~/.config/systemd/user
curl -fsSL \
  https://raw.githubusercontent.com/kartikkabadi/opencode-go-proxy/v0.4.10/contrib/systemd/opencode-go-proxy.service \
  -o ~/.config/systemd/user/opencode-go-proxy.service
systemctl --user daemon-reload
systemctl --user enable --now opencode-go-proxy
curl http://127.0.0.1:8787/health
```

The unit owns the same one listener used on macOS; do not run a second manual
proxy while it is active. For a foreground/manual Linux run, use:

```bash
uvx --from git+https://github.com/kartikkabadi/opencode-go-proxy@v0.4.10 \
  opencode-go-proxy --bind 127.0.0.1 --port 8787
```

Replace `v0.4.10` with the newest tag from
[releases](https://github.com/kartikkabadi/opencode-go-proxy/releases).
For updates, change the pinned `ExecStart` URL in
`contrib/systemd/opencode-go-proxy.service` to the same pinned form, then
`systemctl --user daemon-reload && systemctl --user restart opencode-go-proxy`.

Release cadence: one small patch release per change, so updates arrive
frequently and each one is small.

## Configuration

All flags have environment variable defaults:

| Flag | Env var | Default |
|------|---------|---------|
| `--bind` | `OPENCODE_GO_PROXY_BIND` | `127.0.0.1` |
| `--port` | `OPENCODE_GO_PROXY_PORT` | `8787` |
| `--chat-base-url` | `OPENCODE_GO_BASE_URL` then `OPENCODE_ZEN_BASE_URL` then `CHAT_COMPLETIONS_BASE_URL` | `https://opencode.ai/zen/go/v1` |
| `--api-key-env` | `OPENCODE_GO_PROXY_API_KEY_ENV` | `OPENCODE_GO_API_KEY` |
| `--timeout-sec` | `OPENCODE_GO_PROXY_TIMEOUT_SEC` | `180` |
| `--max-body-mb` | `OPENCODE_GO_PROXY_MAX_BODY_MB` | `20` |

The proxy accepts both `/responses` and `/v1/responses`.

The upstream base URL resolves in this order: the `--chat-base-url` flag, then `OPENCODE_GO_BASE_URL`, then `OPENCODE_ZEN_BASE_URL`, then the legacy `CHAT_COMPLETIONS_BASE_URL`, then the built-in default.

### Remote access

Keep the proxy on loopback whenever possible. A non-loopback bind fails closed
unless both remote mode and a separate caller capability are configured:

```bash
export OPENCODE_GO_PROXY_ALLOW_REMOTE=1
export OPENCODE_GO_PROXY_CALLER_TOKEN="$(openssl rand -hex 32)"
opencode-go-proxy --bind 0.0.0.0
```

Every non-loopback client must send the token in
`X-OpenCode-Go-Proxy-Token`. For a Codex custom provider, pass it from the
environment rather than writing the value into config:

```toml
[model_providers.opencode-go-remote]
name = "OpenCode Go Remote"
base_url = "https://proxy.example.com/v1"
wire_api = "responses"
env_http_headers = { "X-OpenCode-Go-Proxy-Token" = "OPENCODE_GO_PROXY_CALLER_TOKEN" }
```

Use TLS and network-level access controls for any remote deployment. The caller
token is only a proxy access capability; it is never used as or forwarded as a
provider credential. Browser-originated requests remain blocked in remote mode.

**One service and one HTTP port only.** The proxy binds a single listener:
`OPENCODE_GO_PROXY_PORT` (default `8787`). There is no admin port, control
channel, or secondary service. The macOS menu bar app or the Linux systemd user
unit supervises that one process. If something else already listens on the port,
the proxy fails to bind — check with `lsof -nP -iTCP:8787 -sTCP:LISTEN` before
starting another instance. The menu bar app refuses Start when 8787 is owned.

**Short provider name.** The long "opencode go/" label in the Codex model picker comes
from the provider config in `~/.codex/config.toml`, not from this proxy. Shorten it by
editing the provider `name`:

```toml
[model_providers.opencode-go]
name = "Go"  # shows as "Go" instead of "opencode go/"
```

The reference catalog's per-model `display_name` values are already short
("DeepSeek V4 Flash", "Kimi K2.7 Code", etc).

## Ops CLI

All commands run as subcommands of the console script, for example `opencode-go-proxy doctor --json`.

- `doctor [--json] [--fix]` - runs the check set: API key resolution (env or
  keychain), config.toml presence and proxy pointer, catalog readability, port
  free/owned, service health, meter writability, log writability, and best-effort
  upstream reachability. `--fix` repairs only what is safe without writing
  config.toml (log/meter directories, catalog render). Config writes are handled
  by the config-manager step and stay approval-gated.
- `config enable|disable|status [--json]` - owns one marker-commented block in
  `~/.codex/config.toml` (`# BEGIN opencode-go-proxy-managed` to
  `# END opencode-go-proxy-managed`): enable writes `openai_base_url` and
  `model_catalog_json` and never replaces user-owned values, disable removes
  only the managed block (and deletes the file when the block was its only
  content), and Codex Voice realtime keys are preserved on native endpoints
  unless you set them yourself. Point it at another file for testing with
  `OPENCODE_GO_PROXY_CONFIG_PATH`.
- `smoke-test [--base-url URL]` - posts one marker prompt to the local proxy at
  `http://127.0.0.1:8787/v1/responses` and asserts the marker comes back. Point
  it at an isolated scratch proxy with `--base-url` or `OPENCODE_GO_PROXY_BASE_URL`.
- `support-bundle [--output PATH]` - writes the JSON schema v1 diagnostic bundle
  (version, generatedAt, env summary, redacted config snapshot, meter tail, log
  tail, catalog model count, doctor checks) to a mode-600 file. Secret-shaped
  values are redacted.
- `install` - prints how to run the macOS menu bar app (there is no launchd
  agent in 0.3.0).
- `install-skills` - copies the checked-in `opencode-go-proxy` skill into
  `~/.codex/skills/`.
- `refresh-runtime [--force]` - re-runs native model capture and re-renders
  the merged catalog; the menu bar's Refresh Catalog calls this.
- `status [--json]` - reports whether the proxy is running, who owns the port,
  and where logs live.

## Model catalog

The proxy serves `/v1/models` from a runtime catalog in the state dir
(`OPENCODE_GO_PROXY_STATE_DIR`, default `~/.codex/opencode-go-proxy/`). At startup it renders
the state-dir compact catalog (or the checked-in seed at `contrib/opencode-go-models.json`)
immediately, then refreshes in the background: the authenticated OpenCode Go
`/models` endpoint confirms currently available protocol-certified IDs,
TTL-gated so a fresh catalog never hits the network, and the full catalog is
written to `opencode-go-catalog.json` under the state dir. Runtime refresh never
writes the repo's `contrib/` files; maintain the checked-in seed with
`opencode-go-proxy --refresh-catalog`.

Rendered models follow the exact key set Codex reads in codex-router's `merged-models.json`:
`multi_agent_version` lives at the model top level, `comp_hash`/`availability_nux`/`tool_mode`
are present, and `model_messages` carries `approvals`, `collaboration_modes`, `permissions`,
`token_budget`, and `auto_review` instead of dropping them. A parity test pins the renderer to
that key set.

To keep Codex from printing a model metadata warning every turn, point `model_catalog_json` at
a full-shape catalog. The rendered one in the state dir works; copy it where you like:

```bash
mkdir -p ~/.codex/model-catalogs
cp ~/.codex/opencode-go-proxy/opencode-go-catalog.json ~/.codex/model-catalogs/opencode-go.json
```

```toml
model_catalog_json = "/absolute/path/to/.codex/model-catalogs/opencode-go.json"
```

The catalog ships with the `ModelsCache` wrapper (`fetched_at`/`etag`/`client_version`/`models`).
Codex 0.142+ desktop app requires all four top-level fields — a bare `{"models": [...]}` catalog
causes the model picker to fall back to "Custom" instead of showing the full list. The CLI
(`codex debug models`) tolerates the bare format, so this only surfaces in the desktop app.

### Native models and merged catalog

The proxy also captures the models your logged-in Codex account can use
(`codex debug models`), filters out every `opencode-go/` slug, and merges
them with the OpenCode Go catalog into `merged-models.json` in the state dir.
`/v1/models` serves the merged catalog: the app's model picker shows official
GPT models and custom models side by side, with official OAuth untouched.

Routing is by model: a slug in the captured native set goes verbatim to
`https://chatgpt.com/backend-api/codex` (override with
`OPENCODE_GO_PROXY_NATIVE_BASE_URL`), while known Go and Zen catalog entries go
through their translation paths. Unknown IDs are rejected. Run
`refresh-runtime` after logging in or out of an account to recapture.
Only set the native base URL override to an endpoint you trust with the
client's native Codex authorization.

### Local model overlay

Custom models are layered on top of the merged catalog at runtime, never by editing
`contrib/`. `user-models.json` in the state dir holds entries keyed by slug: a full record adds
a model, a partial record edits display fields, `"hide": true` hides one:

```json
{
  "version": 1,
  "models": [
    {"slug": "my-custom-model", "display_name": "My Custom", "context_window": 200000},
    {"slug": "deepseek-v4-flash", "display_name": "Flash (edited)", "priority": 3},
    {"slug": "deepseek-v4-pro", "hide": true}
  ]
}
```

Hidden-model flags from `model-picker.json` (`{"version": 1, "hidden": ["slug"]}`) hide models
from the picker too. Newly added models get a seven-day `availability_nux` announcement,
tracked in `announced-models.json` under the state dir. The proxy re-reads the rendered catalog
on each `/v1/models` request (cached by file mtime), so editing the overlay and refreshing grows
the live list without a restart.

If you want Codex's full `base_instructions` for each model, copy your Codex installation's
bundled `models.json` and append the OpenCode Go entries from the reference catalog (keep the
`ModelsCache` wrapper).

## Trace

Every request emits compact JSON lines on stderr. Important events:

- `server.start`
- `request.received`
- `request.converted`
- `credential.source`
- `upstream.start`
- `upstream.done`
- `response.converted`
- `request.failed`

## Troubleshooting

**Model metadata warning every turn**
Set `model_catalog_json` in Codex config and copy the rendered catalog:
`cp ~/.codex/opencode-go-proxy/opencode-go-catalog.json ~/.codex/model-catalogs/opencode-go.json`

**Connection refused on localhost:8787**
Proxy isn't running. Start it: `opencode-go-proxy` or check `launchctl list | grep opencode`.

**Port already in use**
Only one proxy instance can bind 8787. Confirm what is listening:
`lsof -nP -iTCP:8787 -sTCP:LISTEN`. If a stale instance holds the port, stop it before
starting another.

**API key not found**
Set `OPENCODE_GO_API_KEY` env var or add to macOS keychain:
`security add-generic-password -a "$USER" -s opencode-go-api-key -w`

**Upstream rate limited (429)**
OpenCode Go has 5-hour/weekly/monthly usage limits. Switch to a cheaper model (DeepSeek V4 Flash or MiMo V2.5) to stretch your quota. See [usage limits](https://opencode.ai/docs/go#usage-limits).

**Streaming not working**
Codex sends `stream: true` — the proxy handles this. If you see no SSE events, check stderr trace for `upstream.error` or `upstream.network_error`.

**Codex says "model is not supported when using ChatGPT account"**
Run `opencode-go-proxy config enable`, fully restart Codex, and confirm
`openai_base_url` points at the local proxy. The managed setup uses Codex's
documented proxy configuration so native and routed catalog models can coexist.

## Development

```bash
uv run python -m pytest tests -v
uvx ruff check
uv build
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT. See [LICENSE](LICENSE).
