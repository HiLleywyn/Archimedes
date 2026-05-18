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
  search, image description, remember / recall facts, and deterministic list
  transforms. Every tool result is run through a strict execution pipeline
  before the model sees it (see **Tool execution pipeline**).
- **Lua plugins** -- a full plugin system. A plugin is one `.lua` file that
  can register prefix commands, agent tools, background loops and event
  handlers, and reach out through an HTTP client, a Discord read/write API,
  document and key/value stores, and JSON utilities. Plugins install from a
  GitHub marketplace, survive restarts, and are managed live with
  `.ai plugins`.
- **Productivity** -- private notes, tasks organised into to-do lists, and
  calendar events with reminders, all delivered as bundled Lua plugins.
  Personal items stay private (answered in your DMs); groups let members
  share and collaborate, and any item can be shared, copied, moved or
  transferred between users and groups.
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
| `.note` | everyone | Private notes (answered in your DMs). |
| `.task` | everyone | Tasks and to-do lists, with optional reminders. |
| `.event` | everyone | Calendar events with optional reminders. |
| `.group` | everyone | Create groups, invite members, share and transfer items. |
| `.coinflip` | everyone | Flip a coin (the example plugin). |
| `.ai` | Manage Server | The AI control surface (see `.ai help`). |
| `.ai plugins` | Manage Server | Install, update, enable and disable Lua plugins. |
| `/help` or `.help` | everyone | A menu of sections, every command with examples. |
| `.ping` / `.about` | everyone | Latency and bot info. |

The `.note`, `.task`, `.event`, `.group` and `.coinflip` commands are not
built in -- they come from bundled Lua plugins (see **Lua plugins** below).

### Productivity, privacy and groups

Notes, tasks and events each have an owner. Personal items are yours alone:
the bot replies in your DMs and tidies the command message away, and personal
data follows you across every server. Use `.note share <id> @user [edit]` to
let specific people see one of your items.

A **group** is a shared space. Create one with `.group create <name>`, invite
members with `.group invite <id> @user`, and they accept with
`.group join <id>`. Every member can see and edit the group's items, and group
responses post in the channel so members see them. You can be in many groups.

Targeting and moving items:

- `#<groupid>` at the start of an `add` / `list` argument targets a group
  (for example `.note add #5 Buy supplies`); no `#` means your personal space.
- `~<list>` targets a task list (`.task add ~shopping milk`); the default
  list is `general`.
- `.note copy <id> <dest>` and `.note move <id> <dest>` accept `me`, an
  `@user`, or `#<groupid>` as the destination. `.group duplicate <id>` clones
  a whole group's items into a fresh group you own.
- Reminders: `.task remind <id> in 2h` or `.event remind <id> 2026-06-01 14:30`.
  Times accept relative offsets (`in 30m`, `in 3d`, `in 1w`) or absolute
  `YYYY-MM-DD [HH:MM]` in UTC; a one-minute loop DMs you when one falls due.

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
docker run --env-file .env archimedes
```

A `railway.toml` is included for one-click Railway deploys.

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
| `PREFIX` | no | Command prefix, default `.`. |

## Lua plugins

A plugin is a single `.lua` file. It can register prefix commands (with
nested subcommand groups), agent tools the model can call, and background
loops -- with no Python.

Files in `plugins/` are **bundled** plugins, loaded on every boot: `notes`,
`tasks`, `events` and `groups` are the productivity suite, and `coinflip` is
a worked example. `plugins/README.md` documents the plugin contract and the
`arch` / `ctx` API; the per-plugin document store means a plugin never writes
SQL.

More plugins install from a marketplace -- an ordinary GitHub repository
(`hilleywyn/archimedes-plugins` by default). Server moderators manage every
plugin with `.ai plugins`:

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
database/schema.sql  idempotent schema, applied on boot
plugins/             bundled Lua plugins (notes, tasks, events, groups, coinflip)
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
