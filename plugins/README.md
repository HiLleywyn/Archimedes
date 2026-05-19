# Lua plugins

A plugin is a single `.lua` file. It can register prefix commands (with
nested subcommands), agent tools the model can call, and background loops,
without touching any Python.

Files in this directory are **bundled** plugins: they ship with the bot and
are loaded on every boot. More plugins can be installed from the marketplace
with `.ai plugins install <id>`.

## The plugin contract

A plugin file `return`s one table:

```lua
local M = {}

M.manifest = {
  id          = "myplugin",     -- slug: a-z, 0-9, _ or -, matches the filename
  name        = "My Plugin",
  version     = "1.0.0",
  description = "What it does.",
  author      = "you",
  category    = "General",
  storage     = "myplugin",     -- optional document-store namespace
}

M.commands  = { ... }           -- prefix commands (optional)
M.tools     = { ... }           -- agent tools (optional)
M.loops     = { ... }           -- background jobs (optional)
M.events    = { ... }           -- event handlers (optional)
M.on_load   = function() end    -- runs once when the plugin loads (optional)
M.on_unload = function() end    -- runs once before it unloads (optional)

return M
```

The file's name (without `.lua`) must equal `manifest.id`.

### Commands

```lua
M.commands = {
  {
    name       = "hello",
    aliases    = { "hi" },
    summary    = "Say hello.",          -- shown in .help
    usage      = "hello <name>",        -- shown in .help
    guild_only = false,                 -- reject in DMs when true
    run        = function(ctx) ... end, -- the handler
    subcommands = { ... },              -- same shape, nested up to 2 deep
  },
}
```

A command with `subcommands` becomes a command group; its own `run` fires
when the group is invoked bare.

### Tools

```lua
M.tools = {
  {
    name = "fun.coinflip",
    description = "Flip a coin.",
    parameters = { type = "object", properties = {} },  -- a JSON schema
    handler = function(args, ctx) return { result = "heads" } end,
  },
}
```

A tool `handler` receives `args` (the model's call arguments) and a `ctx`
table describing the call:

| Field | Purpose |
|---|---|
| `ctx.user_id` / `ctx.guild_id` / `ctx.channel_id` | who and where (strings) |
| `ctx.is_dm` | true when the call came from a direct message |
| `ctx.user_name(id)` | resolve a user id to a display name |
| `ctx.store` / `ctx.kv` | the document and key/value stores |
| `ctx.reply(card)` | post an embed into the conversation's channel |
| `ctx.dm(user_id, card)` | DM a user an embed |

A handler returns a table. It is not shown to the model raw: it is wrapped in
the strict tool contract envelope, run through the validation gate,
deterministically compressed and reduced to minimal JSON first. A
`{ error = "..." }` table becomes a structured error result. Keep returned
tables small and flat -- the pipeline bounds anything oversized, but a tight
result reads best.

### Loops

```lua
M.loops = {
  { name = "reminders", interval = 60, run = function() ... end },
}
```

`interval` is in seconds (minimum 15). Loop handlers use the `arch` global.

### Events

A plugin can react to Discord activity and to other plugins. `M.events` maps
an event name to a handler:

```lua
M.events = {
  message      = function(e) ... end,  -- a message was sent
  reaction_add = function(e) ... end,  -- a reaction was added
  member_join  = function(e) ... end,  -- a member joined a server
  member_leave = function(e) ... end,  -- a member left a server
  my_signal    = function(e) ... end,  -- a custom event (see arch.emit)
}
```

The four names above are **gateway events**; their handlers receive:

| Event | Payload fields |
|---|---|
| `message` | `guild_id`, `guild_name`, `channel_id`, `message_id`, `author_id`, `author_name`, `content`, `bot`, `is_dm` |
| `reaction_add` | `guild_id`, `channel_id`, `message_id`, `user_id`, `emoji`, `bot` |
| `member_join` | `guild_id`, `guild_name`, `user_id`, `user_name`, `bot`, `joined_at` |
| `member_leave` | `guild_id`, `guild_name`, `user_id`, `user_name`, `bot` |

Messages from bots (including Archimedes itself) never fire the `message`
event, so a plugin cannot start a feedback loop. Any **other** name in
`M.events` is a custom event delivered by `arch.emit` (see below). Event
handlers should be quick: each runs on its own worker thread.

### Lifecycle hooks

`M.on_load` runs once when the plugin loads and `M.on_unload` runs once before
it unloads. Both are optional and take no arguments. They can use the full
`arch` global. An `on_load` that raises aborts the load, so keep it quick and
defensive.

## The `arch` global

Available inside every handler:

| Field | Purpose |
|---|---|
| `arch.store` | the document store (see below) |
| `arch.kv` | the key/value store (see below) |
| `arch.colors` | named colour ints: `success`, `error`, `info`, `gold`, ... |
| `arch.now()` | current UTC epoch (seconds) |
| `arch.parse_time(text)` | parse `in 2h` / `2026-06-01 14:30` to an epoch, or `nil` |
| `arch.fmt_time(epoch)` | render an epoch as a Discord timestamp |
| `arch.clip(text, n)` | truncate `text` to `n` characters |
| `arch.sigils(text)` | peel `#group` / `~list` tokens: `{group, list, text}` |
| `arch.dm(user_id, card)` | DM a user an embed |
| `arch.user_name(user_id)` | resolve a user id to a display name |
| `arch.log(msg)` | write to the bot log |
| `arch.http` | the outbound HTTP client (see below) |
| `arch.discord` | Discord read/write helpers (see below) |
| `arch.json` | `arch.json.encode(value)` / `arch.json.decode(text)` |
| `arch.base64` | `arch.base64.encode(text)` / `arch.base64.decode(text)` |
| `arch.hash(algo, text)` | hex digest, `algo` is `sha256` / `sha1` / `md5` |
| `arch.uuid()` | a fresh random UUID string |
| `arch.random(a, b)` | random int in `[a, b]`, or a float in `[0, 1)` with no args |
| `arch.emit(name, payload)` | broadcast a custom event (see Events) |

### `arch.http`

A guarded outbound HTTP client:

```lua
local res = arch.http.get("https://api.example.com/data", {
  headers = { Authorization = "Bearer ..." },
  timeout = 8,           -- seconds; capped by the operator
})
if res.ok then
  local data = res.json   -- decoded automatically when the body is JSON
end
```

`arch.http.get(url, opts)`, `arch.http.post(url, opts)` and
`arch.http.request(method, url, opts)` all return a table:
`{ ok, status, body, json, headers, error }`. A refused or failed request
comes back with `ok = false` and an `error` string rather than raising.

`opts` is optional: `headers`, `body` (a string), `json` (a table sent as a
JSON body), `timeout`, `max_bytes`, `max_redirects`. The timeout, size and
redirect values may only lower the operator's caps, never raise them.

Only public `http` / `https` hosts are reachable. Requests to private,
loopback, link-local and other non-public addresses are blocked, and every
redirect hop is checked the same way.

### `arch.discord`

Read and write Discord directly. These act as the bot itself and do **not**
check the calling user's permissions, so treat them as trusted.

| Call | Returns |
|---|---|
| `arch.discord.send(channel_id, card)` | `true` on success |
| `arch.discord.react(channel_id, message_id, emoji)` | `true` on success |
| `arch.discord.history(channel_id, limit)` | array of `{id, author_id, author_name, content, created_at, bot}` |
| `arch.discord.channel(channel_id)` | `{id, name, type, guild_id}` or `nil` |
| `arch.discord.guild(guild_id)` | `{id, name, member_count, owner_id}` or `nil` |
| `arch.discord.member(guild_id, user_id)` | `{id, name, roles, joined_at, bot}` or `nil` |
| `arch.discord.roles(guild_id)` | array of `{id, name, color, position}` |

### `arch.emit`

`arch.emit(name, payload)` broadcasts a custom event to every plugin whose
`M.events` table has a handler for `name`. The handler receives `payload`
with two extra fields injected: `_event` (the name) and `_from` (the id of
the plugin that emitted it). `arch.emit` returns how many plugins received
it.

## The `ctx` table

A command handler receives `ctx`:

| Field | Purpose |
|---|---|
| `ctx.args` | the raw argument string |
| `ctx.author_id` / `ctx.author_name` | the invoking user |
| `ctx.guild_id` / `ctx.channel_id` / `ctx.prefix` | call location |
| `ctx.is_dm` | true in a direct message |
| `ctx.mentions` | array of `{id, name, bot}` |
| `ctx.store` | the document store (same as `arch.store`) |
| `ctx.kv` | the key/value store (same as `arch.kv`) |
| `ctx.reply(card)` | reply with an embed |
| `ctx.ok(msg)` / `ctx.error(msg)` | reply with a success / error embed |
| `ctx.deliver(pages, opts)` | send one card or an array; `opts = {private=true}` DMs it |
| `ctx.confirm(prompt)` | show a yes/no dialog, returns a boolean |
| `ctx.dm(user_id, card)` / `ctx.user_name(id)` | as on `arch` |

**All API calls use a dot, not a colon:** `ctx.reply(...)`, not `ctx:reply(...)`.

Discord ids are passed to Lua as **strings** -- they are too large for a Lua
number to hold exactly.

## The document store

Every plugin gets a namespaced document store (the namespace is
`manifest.storage`, defaulting to `manifest.id`). Plugins in a suite can
share a namespace.

```lua
local id   = arch.store.put("notes", { title = "Buy milk" })  -- returns an id
local rec  = arch.store.get("notes", id)                       -- {id, title, ...}
arch.store.update("notes", id, { title = "Buy oat milk" })
arch.store.delete("notes", id)
local hits = arch.store.query("notes", { title = "Buy milk" }) -- JSON match
local all  = arch.store.all("notes")
```

## The key/value store

Alongside the document store, `arch.kv` is a simple namespaced key/value
store for settings and counters. A value may be any JSON-able Lua value.

```lua
arch.kv.set("greeting", "hello")
local g    = arch.kv.get("greeting")     -- "hello", or nil if unset
arch.kv.delete("greeting")
local keys = arch.kv.keys()              -- array of every key in the namespace
arch.kv.clear()                          -- drop the whole namespace
```

It shares the namespace (`manifest.storage`) with the document store, so a
plugin suite shares its key/value space too.

## Card tables

An embed is a plain table:

```lua
{
  title = "...", description = "...", color = arch.colors.info,
  footer = "...", url = "https://...",
  image = "https://...",      -- a large image below the body
  thumbnail = "https://...",  -- a small image in the top corner
  fields = { { name = "Key", value = "Value", inline = true }, ... },
}
```

`image`, `thumbnail` and `url` accept an `http`/`https` URL only; any other
value is ignored. `url` turns the title into a link. A tool that generates a
picture can post it by replying with a card whose `image` is the result URL.

## Trying it

See `coinflip.lua` for a complete worked example, and the `notes`, `tasks`,
`events` and `groups` plugins for a full suite that shares one namespace.
After editing a bundled file, run `.ai plugins reload <id>`.
