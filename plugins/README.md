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

M.commands = { ... }            -- prefix commands (optional)
M.tools    = { ... }            -- agent tools (optional)
M.loops    = { ... }            -- background jobs (optional)

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
    handler = function(args) return { result = "heads" } end,
  },
}
```

### Loops

```lua
M.loops = {
  { name = "reminders", interval = 60, run = function() ... end },
}
```

`interval` is in seconds (minimum 15). Loop handlers use the `arch` global.

## The `arch` global

Available inside every handler:

| Field | Purpose |
|---|---|
| `arch.store` | the document store (see below) |
| `arch.colors` | named colour ints: `success`, `error`, `info`, `gold`, ... |
| `arch.now()` | current UTC epoch (seconds) |
| `arch.parse_time(text)` | parse `in 2h` / `2026-06-01 14:30` to an epoch, or `nil` |
| `arch.fmt_time(epoch)` | render an epoch as a Discord timestamp |
| `arch.clip(text, n)` | truncate `text` to `n` characters |
| `arch.sigils(text)` | peel `#group` / `~list` tokens: `{group, list, text}` |
| `arch.dm(user_id, card)` | DM a user an embed |
| `arch.user_name(user_id)` | resolve a user id to a display name |
| `arch.log(msg)` | write to the bot log |

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

## Card tables

An embed is a plain table:

```lua
{
  title = "...", description = "...", color = arch.colors.info,
  footer = "...",
  fields = { { name = "Key", value = "Value", inline = true }, ... },
}
```

## Trying it

See `coinflip.lua` for a complete worked example, and the `notes`, `tasks`,
`events` and `groups` plugins for a full suite that shares one namespace.
After editing a bundled file, run `.ai plugins reload <id>`.
