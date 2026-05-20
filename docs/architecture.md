# Archimedes 3.0 architecture

Archimedes 3.0 is two products in one repository:

  1. An **application layer** -- the conversational assistant itself,
     with a persona (Soul), an autonomous loop (Heartbeat), durable
     scheduled tasks, MCP-bridged tools, dynamic UI cards, and a model
     service chain with fallback. Lives under `arch/`.
  2. A **channel layer** -- pluggable transports that carry the
     assistant to a user. Discord is the first channel; web, voice and
     CLI follow the same contract. Lives under `channels/`.

The legacy Discord-specific code (`cogs/`, `ai/`, `framework/`) is the
plumbing both layers stand on. None of it was thrown away; it was lifted
into a coherent application.

```
                         ┌──────────────────────────┐
                         │  Archimedes application  │
                         │                          │
                         │  Soul · Heartbeat · MCP  │
                         │  Memory · Scheduler ·    │
                         │  Services · Dynamic UI   │
                         └────────────┬─────────────┘
                                      │
                            ArchAgent.handle(message, ctx)
                                      │
              ┌───────────────────────┼───────────────────────┐
              │                       │                       │
        ┌─────┴─────┐           ┌─────┴─────┐           ┌─────┴─────┐
        │  Discord  │           │   Web     │           │   CLI     │
        │  channel  │           │  (future) │           │  (future) │
        └───────────┘           └───────────┘           └───────────┘
```

## The application layer (`arch/`)

| Module              | Responsibility                                                    |
| ------------------- | ----------------------------------------------------------------- |
| `arch.core`         | `ArchAgent` -- composes Soul + Memory + Services + Tools.         |
| `arch.config`       | Typed view over `ARCHIMEDES_*` env vars.                          |
| `arch.soul`         | Editable system prompt with named presets.                        |
| `arch.heartbeat`    | Periodic self-check loop and run history.                         |
| `arch.scheduler`    | Cron + oneshot durable scheduled tasks.                           |
| `arch.memories`     | Facade over `ai.memory.MemoryService` (verb-named).               |
| `arch.mcp`          | HTTP and stdio MCP client + registry.                             |
| `arch.services`     | Multi-provider service chain with circuit breakers.               |
| `arch.dynamic_ui`   | Channel-agnostic UI primitives: `Card`, `Section`, `Button`, ...  |
| `arch.tools.builtin`| Archimedes built-in tools: `arch.time`, `.location`, `.fetch_url`.|

### The agent contract

```python
from arch import ArchAgent, ChannelContext

agent = ArchAgent(
    config=ArchConfig.from_env(),
    db=bot.db,
    memory_service=bot.memory,
    tool_registry=bot.tools,
)
await agent.start()

ctx = ChannelContext(
    session_key="arch:discord:channel:123",
    transport="discord",
    user_id=42, guild_id=7, channel_id=123,
)
response = await agent.handle("hello", ctx)
print(response.text)
```

`ArchAgent.start()` opens scheduled-task and heartbeat loops, connects
declared MCP servers, and starts the service chain. `ArchAgent.stop()`
unwinds all of it cleanly.

## The channel layer (`channels/`)

A channel implements `start`, `stop`, and `dispatch(text, ctx) ->
ArchResponse`. The base class wraps `ArchAgent.handle`; subclasses add
transport-specific concerns:

| Channel              | Concerns                                                        |
| -------------------- | --------------------------------------------------------------- |
| `DiscordChannel`     | DM and guild policy, session key derivation, embed rendering.   |
| `NullChannel`        | The no-op transport used by the test harness.                   |

### Session keys

Every routable conversation gets a stable id following the OpenClaw
"channels" convention:

  * `arch:discord:dm:<user_id>`
  * `arch:discord:channel:<channel_id>`
  * `arch:discord:thread:<thread_id>`

`channels.session.parse_session_key` round-trips a key back into its
scheme / transport / kind / id parts for diagnostics.

### Policies

`evaluate_dm` and `evaluate_guild` apply the configured DM and guild
policies (`open`, `allowlist`, `disabled`) before the agent ever sees an
inbound message. The bot owner is always allowed in DMs; an unknown
policy falls back to `allowlist` so a config typo never opens the bot
up.

## Database additions

The 3.0 schema adds five idempotent tables to `database/schema.sql`:

  * `archimedes_soul` -- the active soul prompt (one row per scope)
  * `archimedes_heartbeat_log` -- recent-activity history
  * `archimedes_scheduled_tasks` -- durable scheduler queue
  * `archimedes_mcp_servers` -- runtime-added MCP servers
  * `archimedes_settings` -- operator-set application toggles

All existing tables stay untouched, so a 2.x deployment upgrades in
place.

## Environment variables

Every 3.0 setting defaults to a no-op so an unchanged deployment behaves
exactly as before. See `.env.example` for the full block; the headline
variables are:

  * `ARCHIMEDES_SOUL`, `ARCHIMEDES_SOUL_PRESET`
  * `ARCHIMEDES_HEARTBEAT_ENABLED` (and the interval / window pair)
  * `ARCHIMEDES_SCHEDULER_ENABLED`
  * `ARCHIMEDES_MCP_SERVERS` (comma-separated declarations)
  * `ARCHIMEDES_SERVICES` (ordered provider list with fallback)
  * `DISCORD_DM_POLICY`, `DISCORD_GUILD_POLICY`

## Operator surface

The new `.app` command group (owner-only) controls every 3.0 feature
from Discord without leaving the chat:

  * `.app soul` -- show the active soul; `.app soul preset <name>` /
    `.app soul set <text>` / `.app soul reset`
  * `.app heartbeat` -- show recent runs; `.app heartbeat run` fires
    one immediately
  * `.app schedule list/add/cron/cancel`
  * `.app mcp` -- list servers; `.app mcp add <name> <url>` /
    `.app mcp remove <name>`
  * `.app services` -- show the model fallback chain health

## What did not change

  * `cogs/chat.py` and the streaming tool loop in `ai/tools.py` still own
    the heavy chat path. The application layer wraps the model surface
    they call into; it does not replace them.
  * `ai/memory.py` keeps its tables and refresh loop; `arch.memories`
    is a verb-renamed facade.
  * The Node sidecar (`agent-sidecar/`) keeps its place. The service
    chain picks it first when configured; if it errors, the chain falls
    through to other providers.
  * The Lua plugin runtime (`framework/plugins/`) is unchanged. A future
    iteration adds an `arch.kai`-style namespace inside Lua so plugins
    can emit dynamic UI cards directly.

## Testing

`pytest tests/` runs 237 offline tests, including 59 covering the new
application and channel layers. No Postgres or Discord token required.
