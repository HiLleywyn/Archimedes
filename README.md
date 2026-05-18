# Archimedes

A standalone AI chat bot for Discord. Archimedes is a memory-backed conversational
companion: mention it, reply to it, or use `.ask`, and it answers with a
persona-driven model while learning who it is talking to.

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
  messages, the `.ask` command, and optional ambient chime-ins.
- **Streaming replies** with a live status spinner and Regenerate / Continue
  buttons.
- **Context learning** -- it builds and refreshes a per-user memory summary, a
  time-decayed trait profile (curious, technical, blunt, upbeat, ...), durable
  key/value facts, and per-channel activity context. Every reply gets richer.
- **Tool calling** -- the model can call generic, non-financial tools:
  web search, image description, and remember / recall facts. More tools can
  be added with Lua plugins (`plugins/`).
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
| `.ai` | Manage Server | The AI control surface (see `.ai help`). |
| `.help` / `.ping` / `.about` | everyone | Bot meta. |

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

Drop a `.lua` file in `plugins/` to register extra agent tools without
touching Python. See `plugins/README.md` and the `plugins/coinflip.lua`
example. Run `.ai reloadtools` after adding one.

## Layout

```
main.py              entry point
config.py            env-driven configuration
pyproject.toml       project metadata + pytest config
requirements.txt     runtime dependencies
framework/           bot class, embeds, UI, context, DB layer, audit
ai/                  model client, memory, traits, context, tools, safety
cogs/                chat brain, .arch, .ai admin, memory sidecar, meta
database/schema.sql  idempotent schema, applied on boot
plugins/             Lua tool plugins (+ a working coinflip example)
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
