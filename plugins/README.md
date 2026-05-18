# Lua plugins

Drop a `.lua` file in this directory to register extra agent tools without
touching Python. Each file must `return` a table (array) of tool
definitions. Every definition needs:

| field         | type     | description                                  |
|---------------|----------|----------------------------------------------|
| `name`        | string   | Tool name, e.g. `fun.coinflip`.              |
| `description` | string   | What the tool does (the model reads this).   |
| `parameters`  | table    | A JSON-schema `object` describing the args.  |
| `handler`     | function | `function(args)` returning a result table.   |

Handlers run synchronously in a worker thread, so a slow plugin never
blocks the bot. After adding or editing a file, run `.ai reloadtools`.

Lua plugin support needs the optional `lupa` package (already in
`requirements.txt`). When `lupa` is missing the loader simply logs a notice
and the bot runs with the built-in tools only.

See `coinflip.lua` for a complete working example.
