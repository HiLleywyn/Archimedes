# Archimedes

A standalone AI chat bot for Discord. Archimedes is a memory-backed conversational
companion: mention it, reply to it, use `.ask`, or just message it directly,
and it answers with a persona-driven model while learning who it is talking to.

It is fully self-contained: the `.ai` and `.arch` command groups, per-user /
per-channel / per-server context learning, the tool-calling loop and the
memory sidecar all ship with their own framework, config, database schema and
entry point. There are no external service dependencies beyond the model
provider, PostgreSQL and (optionally) Redis.

This bot lives at the root of its own repository. `main.py` is the entry
point, `requirements.txt` / `pyproject.toml` declare the dependencies,
`Dockerfile` and `railway.toml` deploy it, and `.github/workflows/ci.yml`
runs the test suite.

## What it does

- **Conversational chat** -- replies to `@mentions`, replies to its own
  messages, direct messages, the `.ask` command, and optional ambient
  chime-ins.
- **Streaming replies** with a live status spinner and Regenerate / Continue
  buttons.
- **Context learning** -- it builds and refreshes a per-user memory summary, a
  time-decayed trait profile (curious, technical, blunt, upbeat, ...), durable
  key/value facts, and per-channel activity context. Every reply gets richer.
- **Tool calling** -- the model can call generic, non-financial tools: web
  search, image description, image and video generation, remember / recall
  facts, deterministic list transforms, and a sandboxed file workspace (read,
  write, list, regex grep, and an allowlist shell). Image and video
  generation run on OpenRouter and are retunable per server with
  `.ai model set image|video`. Every tool result is run through a strict
  execution pipeline before the model sees it (see **Tool execution
  pipeline**).
- **File workspace** -- the `files.*` and `shell.run` tools give the model a
  private scratch directory, one per server (per user in a DM). It is
  sandboxed: paths cannot escape it, files and total size are capped, and the
  shell runs only an allowlist of read-only commands -- it searches a
  directory or the whole workspace with ripgrep and a single named file with
  grep. Configured with the `WORKSPACE_*` variables; turn it off entirely
  with `WORKSPACE_ENABLED`.
- **Lua plugins** -- a full plugin system. A plugin is one `.lua` file that
  can register prefix commands, agent tools, background loops and event
  handlers, and reach out through an HTTP client, a Discord read/write API,
  document and key/value stores, and JSON utilities. The `coinflip` plugin
  ships bundled as a worked example; more plugins -- a notes, tasks, events
  and groups productivity suite among them -- install from a GitHub
  marketplace, survive restarts, and are managed live with `.ai plugins`.
- **Memory sidecar** -- long-term facts and episodes, passive learning in
  opted-in channels, and an append-only training corpus of every turn.
- **Thread or inline replies** -- each member picks their style with
  `.arch chat` / `.arch threads`.
- **Staff control surface** -- `.ai` tunes feature flags, system prompts,
  persona, the per-guild model picker, web search backend, the tool registry,
  the emoji meaning index, and an audit feed.
- **Prompt-injection defence** and output sanitisation on every turn.

There is **no crypto, money or economy** anything. There is no premium gate
and no unlock requirement -- chat is open to everyone; the `.ai` staff
commands require the Manage Server permission.

## Commands

| Command | Who | What |
|---|---|---|
| `@Archimedes <message>` | everyone | Talk to Archimedes. |
| `.ask <question>` | everyone | Ask Archimedes something. |
| `.arch` (or `.a`) | everyone | Tune how Archimedes talks to you. |
| `.arch chat` / `threads` | everyone | Inline vs thread replies. |
| `.arch ctx [@user\|server\|clear]` | everyone | Inspect / wipe learned context. |
| `.arch save` / `saved` / `unsave` | everyone | Bookmark Archimedes answers. |
| `.arch optin` / `optout` | everyone | AI context tracking. |
| `.coinflip` | everyone | Flip a coin (the bundled example plugin). |
| `.ai` | Manage Server | The AI control surface (see `.ai help`). |
| `.ai plugins` | Manage Server | Install, update, enable and disable Lua plugins. |
| `/help` or `.help` | everyone | A menu of sections, every command with examples. |
| `.ping` / `.about` | everyone | Latency and bot info. |

The `.coinflip` command is not built in -- it comes from the bundled
`coinflip` Lua plugin (see **Lua plugins** below). Productivity commands like
`.note`, `.task`, `.event` and `.group` install from the plugin marketplace.

## Setup

1. Create a Discord application + bot. Enable the **Message Content** and
   **Server Members** privileged intents.
2. Provision a PostgreSQL database. Redis is optional (short-term memory).
3. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`, `DATABASE_URL`
   and `OPENROUTER_API_KEY`.
4. Install and run:

```sh
pip install -r requirements.txt
python main.py
```

The database schema (`database/schema.sql`) is applied automatically on every
boot -- it is idempotent.

### Docker

```sh
docker build -t archimedes .
docker run --env-file .env -v archimedes-data:/data archimedes
```

The agent file workspace lives under `/data` (`WORKSPACE_ROOT` is set to
`/data/workspace` in the image), so mounting a volume there keeps it across
restarts; without `-v` the workspace is ephemeral.

A `railway.toml` is included for one-click Railway deploys. On Railway,
attach a volume to the service with the mount path `/data` to get the same
persistence.

## Configuration

Every setting is an environment variable; see `.env.example` for the full,
documented list. The essentials:

| Variable | Required | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | yes | Bot token. |
| `DATABASE_URL` | yes | PostgreSQL connection string. |
| `OPENROUTER_API_KEY` | yes (openrouter) | Model provider key. |
| `OPENROUTER_MODEL` | no | Default chat model slug. |
| `REDIS_URL` | no | Enables the short-term memory store. |
| `CHAT_BACKEND` | no | `openrouter` (default) or `ollama`. |
| `SEARCH_BACKEND` | no | `ddg` (default, no key) or `brave`. |
| `AGENT_SIDECAR_ENABLED` | no | Run the tool loop on the Agent SDK sidecar (default on). |
| `WORKSPACE_ENABLED` | no | Sandboxed file + shell tools for the agent (default on). |
| `PREFIX` | no | Command prefix, default `.`. |

## Lua plugins

A plugin is a single `.lua` file. It can register prefix commands (with
nested subcommand groups), agent tools the model can call, and background
loops -- with no Python.

The `plugins/` directory holds the **bundled** plugins, loaded on every
boot. Only `coinflip` ships bundled -- a small, complete worked example.
`plugins/README.md` documents the plugin contract and the `arch` / `ctx`
API; the per-plugin document store means a plugin never writes SQL.

More plugins install from a marketplace -- an ordinary GitHub repository
(`hilleywyn/archimedes-plugins` by default) -- among them a notes, tasks,
events and groups productivity suite. Server moderators manage every plugin
with `.ai plugins`:

| Command | What |
|---|---|
| `.ai plugins` / `list` | Installed plugins and their state. |
| `.ai plugins search [query]` | Browse the marketplace. |
| `.ai plugins info <id>` | One plugin's manifest, commands and tools. |
| `.ai plugins install <id>` | Install a plugin from the marketplace. |
| `.ai plugins uninstall <id>` | Remove a marketplace plugin. |
| `.ai plugins enable` / `disable <id>` | Load or unload a plugin live. |
| `.ai plugins update [id]` | Pull the latest version. |
| `.ai plugins reload [id]` | Recompile and reload from source. |

Installed and enabled plugins persist across restarts: bundled plugins ship
in the repository, and a marketplace plugin's Lua source is stored in the
database, so a redeploy of the (otherwise stateless) container restores the
exact plugin set.

## Agent loop

A chat turn that calls tools is a multi-step loop: the model asks for a tool,
the tool runs, its result is fed back, and the model is asked again -- until
it answers in plain text or a stop condition fires.

That loop runs through the **OpenRouter Agent SDK**. The SDK is a TypeScript
package, so it lives in a small Node service -- the **agent sidecar** in
`agent-sidecar/` -- that the Python bot drives over a WebSocket. The bot
streams the model's text back, and every tool the SDK calls is bridged back
to the bot, which runs it through the tool registry and the execution
pipeline (below) before the result returns to the model. Tools, plugins and
the pipeline stay exactly where they are; only the loop moves.

The sidecar autostarts with the bot -- the Docker image bundles the Node
runtime and the built service -- and is supervised by it. If it is
unreachable the bot falls back to an equivalent in-process loop, so the
feature is safe to leave on. Stop conditions are tunable: `AGENT_MAX_STEPS`
caps model steps per turn and `AGENT_MAX_COST` sets an optional per-turn
dollar ceiling.

Two within-turn controls run on top of that loop. A tool may return a
`next_turn` block in its result to steer the following model turn -- a
different model, a lower temperature, a tighter token budget, or extra
instructions -- which both the sidecar (through the Agent SDK's
`nextTurnParams`) and the in-process loop apply before the model is asked
again. And a tool call can be gated on human approval: list tool names in
`AGENT_APPROVAL_TOOLS` or risk tiers in `AGENT_APPROVAL_RISKS`, and a gated
call posts an Approve / Reject prompt in the channel before it runs, with a
rejected call handed back to the model as a declined result. Approval is
resolved entirely on the bot side within the turn, so the sidecar stays
stateless per turn and conversation state stays in the bot.

## Tool execution pipeline

A tool result never goes straight from a handler to the model. It travels a
fixed, layered path, and every layer is deterministic machinery:

```
raw tool return
  -> envelope     wrap into the strict contract shape
  -> validation   the Pydantic gate: pass, or become a structured error
  -> processing   schema filter, deterministic compression
  -> injection    strip internal noise, emit minimal clean JSON
  -> the model
```

The **contract** is one fixed envelope -- `status`, `tool`, `version`,
`data`, `error`, `meta` -- that every downstream stage assumes is exact. The
**validation gate** is a hard Pydantic barrier: a malformed or drifted
envelope is rejected outright and replaced with a structured error, so it
never reaches the model. The **processing** stage filters a result to the
fields its tool declared and compresses it deterministically -- bounding
string length, list size and nesting depth -- with every trim recorded as a
note. The **injection** formatter strips the contract version, timing and
other internal bookkeeping and emits the smallest clean JSON that still
answers the question, hard-capped so one tool result can never blow the
context window.

`transform.slice`, `transform.project` and `transform.aggregate` round this
out: pure, non-model tools for the list work the model would otherwise do by
eye -- top-N, field selection, and sum / min / max / mean / count.

A tool whose output *is* the point -- a workspace file read, a shell capture
-- is marked **verbatim**: it skips string and list compression and uses a
much larger injection ceiling, so the model sees the file or command output
whole instead of trimmed to a snippet.

The pipeline lives in `framework/pipeline/`; the compression caps are tunable
through the `PIPELINE_*` environment variables.

## Layout

```
main.py              entry point
config.py            env-driven configuration
pyproject.toml       project metadata + pytest config
requirements.txt     runtime dependencies
framework/           bot class, embeds, UI, context, DB layer, audit
framework/plugins/   the Lua plugin system: runtime, API, registry, manager
framework/pipeline/  the tool-execution pipeline: envelope, gate, processing
ai/                  model client, memory, traits, context, tools, safety
cogs/                chat brain, .arch, .ai admin, sidecar, meta
agent-sidecar/       the OpenRouter Agent SDK service (TypeScript / Node)
database/schema.sql  idempotent schema, applied on boot
plugins/             bundled Lua plugins (coinflip, the worked example)
tests/               offline smoke tests
.github/workflows/   CI (lint + tests)
Dockerfile           container build
```

## Tests

```sh
pip install -r requirements-dev.txt
python -m pytest tests/
```

The suite is fully offline -- it needs no Discord token, database or model
key. It checks that every module imports, the cogs register without
collisions, and the sanitizers / injection detection / trait engine / tool
registry / prompt assembly behave.
