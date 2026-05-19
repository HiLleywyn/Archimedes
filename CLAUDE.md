# Archimedes -- Claude Code Guidelines

## Hard rules -- never break these

### Git commit hygiene -- commits land only under HiLleywyn
- **Always** set both author AND committer to `HiLleywyn <lleywyn@proton.me>`
  on every commit. Use
  `git -c user.name="HiLleywyn" -c user.email="lleywyn@proton.me" commit`
  or equivalent.
- **Never** leave `Claude <noreply@anthropic.com>` -- or any other AI /
  assistant identity -- as the author or committer on any commit.
- **Never** include `https://claude.ai/code/session_*` links in commit
  messages.
- **Never** add a "Generated with Claude Code" line, a session link, or any
  similar trailer at the bottom of a commit message.

### Branch naming -- version, then type, then name
- **Never** prefix a branch with `claude/` -- or any other AI / assistant
  name.
- Name every branch `version/type/name`: the version first, then the change
  type (`feat`, `fix`, `docs`, `chore`, ...), then a short slug. For example
  `v0.0.1/feat/ripgrep-coinflip-defaults`. The trailing name may be dropped
  for a trivial change (`v0.0.1/feat`). Versions start at `v0.0.1`.

### No AI attribution anywhere
Nothing committed to this repository may advertise that it was produced with
AI assistance. No `Co-Authored-By: Claude` trailers, no "Generated with
Claude Code" lines, no AI or assistant tool names in commit messages, pull
request titles or bodies, code comments, or documentation. Everything reads
as if written by HiLleywyn.

### No em dashes, en dashes, or Unicode minus signs in source files
Use plain ASCII hyphens only. These characters have caused silent failures in
string matching and shell scripts, and they are a giveaway of
machine-generated text. Run a check before committing.

### No legacy naming or code
Never leave legacy names, column names, variable names, or dead code in
place. If something was named for a system that no longer exists, rename it
immediately. When removing a feature, search the ENTIRE codebase for every
reference and remove ALL of them -- code, strings, help text, docs, config
lookups, function parameters, error messages. If it is removed, it is gone
from everywhere.
