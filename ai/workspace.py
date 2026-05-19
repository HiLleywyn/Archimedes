"""ai/workspace.py -- the sandboxed per-server file workspace.

Backs the ``files.*`` and ``shell.run`` agent tools. The bot's AI agent is
driven by whatever a Discord user types, so in effect every one of these
tools is callable by an untrusted caller. The sandbox here is what makes that
safe to expose:

  * Every path a tool is handed is resolved against one workspace root and
    confined to a per-server subdirectory (per user in a DM). ``..``, absolute
    paths and symlinks that would climb out are rejected -- a tool can never
    read the bot's own ``.env``, reach another server's files, or escape.
  * Files are size-capped, the directory is quota-capped, and the file count
    is capped, so the workspace can never be used to fill the host disk.
  * ``shell.run`` executes only an allowlist of read-only commands, with no
    shell interpreter (so pipes, redirects and ``;`` are not features), no
    inherited environment, the workspace as its working directory, stdin
    closed, and a hard timeout.

Every public function is total: it never raises. Bad input comes back as
``{"error": "..."}``, which the tool pipeline turns into a clean error
envelope, exactly like the deterministic transform functions.
"""
from __future__ import annotations

import asyncio
import re
import shlex
import time
from pathlib import Path

from config import Config


class WorkspaceError(Exception):
    """A workspace operation was rejected: bad path, over a cap, or denied."""


# ── workspace layout ──────────────────────────────────────────────────────────
def _repo_root() -> Path:
    """The bot's own directory -- the parent of this ``ai/`` package."""
    return Path(__file__).resolve().parent.parent


def workspace_root() -> Path:
    """The base directory every per-server workspace lives under.

    ``WORKSPACE_ROOT`` when set; otherwise ``.workspace/`` beside the bot.
    """
    configured = (Config.WORKSPACE_ROOT or "").strip()
    if configured:
        return Path(configured).expanduser()
    return _repo_root() / ".workspace"


def _namespace(ctx) -> str:
    """The sandbox bucket for one caller: per-guild, or per-user in a DM."""
    try:
        guild_id = int(getattr(ctx, "guild_id", 0) or 0)
    except (TypeError, ValueError):
        guild_id = 0
    try:
        user_id = int(getattr(ctx, "user_id", 0) or 0)
    except (TypeError, ValueError):
        user_id = 0
    if guild_id:
        return f"g{guild_id}"
    if user_id:
        return f"u{user_id}"
    return "shared"


def namespace_dir(ctx) -> Path:
    """The caller's own workspace directory, created on first use."""
    ns_dir = workspace_root() / _namespace(ctx)
    ns_dir.mkdir(parents=True, exist_ok=True)
    return ns_dir


# ── caps ──────────────────────────────────────────────────────────────────────
def _max_file_bytes() -> int:
    return max(1, Config.WORKSPACE_MAX_FILE_KB) * 1024


def _quota_bytes() -> int:
    return max(1, Config.WORKSPACE_QUOTA_KB) * 1024


def _to_int(value, default: int) -> int:
    """Coerce a tool argument to an int, or fall back to ``default``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dir_stats(ns_dir: Path) -> tuple[int, int]:
    """Total bytes and regular-file count currently under ``ns_dir``."""
    total = 0
    count = 0
    for path in ns_dir.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                total += path.stat().st_size
            except OSError:
                continue
            count += 1
    return total, count


# ── path confinement ──────────────────────────────────────────────────────────
def _resolve(ns_dir: Path, rel: str) -> Path:
    """Resolve ``rel`` inside ``ns_dir``, rejecting anything that escapes it.

    ``..``, absolute paths, ``~`` and symlinks that would climb out all raise
    :class:`WorkspaceError`. The returned path is fully resolved and proven to
    sit inside ``ns_dir``.
    """
    text = str(rel or "").strip()
    if not text:
        raise WorkspaceError("a path is required")
    if text.startswith(("/", "~")) or "\\" in text:
        raise WorkspaceError("path must be relative to the workspace")
    resolved = (ns_dir / text).resolve()
    base = ns_dir.resolve()
    if resolved != base and base not in resolved.parents:
        raise WorkspaceError("path escapes the workspace")
    return resolved


def _rel(target: Path, base: Path) -> str:
    """``target`` written relative to ``base`` with forward slashes."""
    return target.relative_to(base).as_posix() or "."


# ── file tools ────────────────────────────────────────────────────────────────
def read_file(ctx, path: str, offset=None, limit=None) -> dict:
    """Return the UTF-8 text contents of a workspace file.

    With neither ``offset`` nor ``limit`` the whole file comes back. Pass
    ``offset`` (a 1-based line number) and/or ``limit`` (a line count) to read
    an explicit window of a large file instead. The result always reports
    ``total_lines`` and whether ``more`` lines follow the window, so a big
    file is read deliberately -- never trimmed by surprise.
    """
    try:
        ns_dir = namespace_dir(ctx)
        target = _resolve(ns_dir, path)
    except WorkspaceError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"workspace unavailable: {exc}"}
    if not target.exists():
        return {"error": f"no such file: {path}"}
    if target.is_dir():
        return {"error": f"{path} is a directory, not a file"}
    try:
        size = target.stat().st_size
        if size > _max_file_bytes():
            return {"error": f"file is {size} bytes, over the "
                             f"{_max_file_bytes()} byte read limit"}
        raw = target.read_bytes()
    except OSError as exc:
        return {"error": f"could not read {path}: {exc}"}

    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()
    total_lines = len(lines)
    rel = _rel(target, ns_dir.resolve())
    if offset is None and limit is None:
        # No window asked for: hand back the file exactly as it is on disk.
        return {
            "path": rel, "content": text, "bytes": size,
            "total_lines": total_lines, "start_line": 1,
            "lines_returned": total_lines, "more": False,
        }
    start = max(1, _to_int(offset, 1))
    if limit is None:
        window = lines[start - 1:]
    else:
        window = lines[start - 1:start - 1 + max(0, _to_int(limit, 0))]
    return {
        "path": rel, "content": "\n".join(window), "bytes": size,
        "total_lines": total_lines, "start_line": start,
        "lines_returned": len(window),
        "more": start - 1 + len(window) < total_lines,
    }


def write_file(ctx, path: str, content, mode: str = "overwrite") -> dict:
    """Create or update a workspace file with ``content``.

    ``mode`` is ``overwrite`` (the default) or ``append``. The write is
    refused when it would push the file past the per-file size limit, the
    workspace past its quota, or the directory past its file-count limit.
    """
    if not isinstance(content, str):
        return {"error": "content must be a string"}
    mode = str(mode or "overwrite").strip().lower()
    if mode not in ("overwrite", "append"):
        return {"error": "mode must be 'overwrite' or 'append'"}
    try:
        ns_dir = namespace_dir(ctx)
        target = _resolve(ns_dir, path)
    except WorkspaceError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"workspace unavailable: {exc}"}
    if target.is_dir():
        return {"error": f"{path} is a directory"}

    payload = content.encode("utf-8")
    existing = 0
    if target.exists():
        try:
            existing = target.stat().st_size
        except OSError:
            existing = 0
    final_size = existing + len(payload) if mode == "append" else len(payload)
    if final_size > _max_file_bytes():
        return {"error": f"the file would be {final_size} bytes, over the "
                         f"{_max_file_bytes()} byte per-file limit"}

    total, count = _dir_stats(ns_dir)
    if total - existing + final_size > _quota_bytes():
        return {"error": f"the workspace quota of {_quota_bytes()} bytes "
                         "would be exceeded; delete a file first"}
    creating = not target.exists()
    if creating and count >= max(1, Config.WORKSPACE_MAX_FILES):
        return {"error": "the workspace already holds the maximum of "
                         f"{Config.WORKSPACE_MAX_FILES} files"}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a" if mode == "append" else "w",
                  encoding="utf-8") as handle:
            handle.write(content)
        written = target.stat().st_size
    except OSError as exc:
        return {"error": f"could not write {path}: {exc}"}
    return {
        "path": _rel(target, ns_dir.resolve()),
        "bytes": written,
        "mode": mode,
        "created": creating,
    }


def list_dir(ctx, path: str = "") -> dict:
    """List the immediate contents of a workspace directory."""
    try:
        ns_dir = namespace_dir(ctx)
        base = ns_dir.resolve()
        target = _resolve(ns_dir, path) if str(path or "").strip() else base
    except WorkspaceError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"workspace unavailable: {exc}"}
    if not target.exists():
        return {"error": f"no such directory: {path}"}
    if not target.is_dir():
        return {"error": f"{path} is a file, not a directory"}
    entries = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name):
            is_dir = child.is_dir()
            size = 0
            if not is_dir:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = 0
            entries.append({
                "name": child.name,
                "type": "dir" if is_dir else "file",
                "bytes": size,
            })
    except OSError as exc:
        return {"error": f"could not list {path}: {exc}"}
    return {"path": _rel(target, base), "entries": entries,
            "count": len(entries)}


_GREP_MAX_MATCHES = 100
_GREP_LINE_CHARS = 240


def grep_files(ctx, pattern: str, path: str = "",
               ignore_case: bool = False) -> dict:
    """Search workspace files for lines matching a regular expression.

    Without ``path`` the whole workspace is searched; with it the search is
    limited to that file or subdirectory. Binary files are skipped.
    """
    text = str(pattern or "")
    if not text:
        return {"error": "pattern is required"}
    try:
        regex = re.compile(text, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return {"error": f"invalid regular expression: {exc}"}
    try:
        ns_dir = namespace_dir(ctx)
        base = ns_dir.resolve()
        root = _resolve(ns_dir, path) if str(path or "").strip() else base
    except WorkspaceError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"workspace unavailable: {exc}"}
    if not root.exists():
        return {"error": f"no such file or directory: {path}"}

    if root.is_file():
        files = [root]
    else:
        files = sorted(p for p in root.rglob("*")
                       if p.is_file() and not p.is_symlink())
    matches: list[dict] = []
    searched = 0
    truncated = False
    max_bytes = _max_file_bytes()
    for file_path in files:
        try:
            if file_path.stat().st_size > max_bytes:
                continue
            raw = file_path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw:
            continue  # a binary file -- nothing a text search should report
        searched += 1
        rel = _rel(file_path, base)
        for lineno, line in enumerate(
                raw.decode("utf-8", "replace").splitlines(), 1):
            if regex.search(line):
                matches.append({"file": rel, "line": lineno,
                                "text": line[:_GREP_LINE_CHARS]})
                if len(matches) >= _GREP_MAX_MATCHES:
                    truncated = True
                    break
        if truncated:
            break
    return {
        "pattern": text,
        "matches": matches,
        "match_count": len(matches),
        "files_searched": searched,
        "truncated": truncated,
    }


def delete_file(ctx, path: str) -> dict:
    """Delete a workspace file. Directories are left alone."""
    try:
        ns_dir = namespace_dir(ctx)
        target = _resolve(ns_dir, path)
    except WorkspaceError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"workspace unavailable: {exc}"}
    if not target.exists():
        return {"error": f"no such file: {path}"}
    if target.is_dir():
        return {"error": f"{path} is a directory; only files can be deleted"}
    rel = _rel(target, ns_dir.resolve())
    try:
        target.unlink()
    except OSError as exc:
        return {"error": f"could not delete {path}: {exc}"}
    return {"path": rel, "deleted": True}


# ── allowlist shell tool ──────────────────────────────────────────────────────
# Read-only commands only. The command is executed directly (no shell), so a
# pipe or redirect is never interpreted -- it is just a literal argument.
_SHELL_ALLOWLIST = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "find",
    "pwd", "echo", "date", "stat", "sort", "uniq", "cut", "tr", "nl",
    "basename", "dirname", "du", "diff",
})
# find actions (and their write-to-file siblings) run another program or
# write outside a relative path -- never allowed, whatever the command is.
_BLOCKED_TOKENS = frozenset({
    "-exec", "-execdir", "-ok", "-okdir", "-delete",
    "-fprint", "-fprint0", "-fprintf", "-fls",
})
_SHELL_METACHARS = (";", "|", "&", ">", "<", "`", "$(", "${", "\n")
_SHELL_OUTPUT_CHARS = 64000
_SHELL_MAX_COMMAND_CHARS = 600
_SHELL_MAX_ARGS = 40


def _unsafe_arg(arg: str) -> bool:
    """True when a shell argument could reach outside the workspace."""
    if not arg:
        return False
    if arg.startswith(("/", "~")):
        return True
    return ".." in arg.replace("\\", "/").split("/")


async def run_shell(ctx, command: str) -> dict:
    """Run one allowlisted, read-only command inside the workspace."""
    raw = str(command or "").strip()
    if not raw:
        return {"error": "command is required"}
    if len(raw) > _SHELL_MAX_COMMAND_CHARS:
        return {"error": "command is too long"}
    for meta in _SHELL_METACHARS:
        if meta in raw:
            return {"error": "shell operators (pipes, redirects, ;, &&, "
                             "backticks, $()) are not supported; run a "
                             "single simple command"}
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        return {"error": f"could not parse the command: {exc}"}
    if not argv:
        return {"error": "command is required"}
    if len(argv) > _SHELL_MAX_ARGS:
        return {"error": "too many arguments"}

    program = argv[0]
    if program not in _SHELL_ALLOWLIST:
        return {"error": f"'{program}' is not an allowed command. Allowed: "
                         + ", ".join(sorted(_SHELL_ALLOWLIST))}
    for arg in argv[1:]:
        if arg in _BLOCKED_TOKENS:
            return {"error": f"the '{arg}' option is not allowed"}
        if _unsafe_arg(arg):
            return {"error": f"argument '{arg}' is not allowed: paths must "
                             "be relative and stay inside the workspace"}

    try:
        ns_dir = namespace_dir(ctx)
    except OSError as exc:
        return {"error": f"workspace unavailable: {exc}"}
    timeout = max(1, Config.WORKSPACE_SHELL_TIMEOUT_S)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(ns_dir),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(ns_dir), env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        return {"error": f"could not run the command: {exc}"}
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except (OSError, ValueError):
            pass
        return {"error": f"the command did not finish within {timeout}s"}
    out = stdout.decode("utf-8", "replace")
    err = stderr.decode("utf-8", "replace")
    return {
        "command": raw,
        "exit_code": proc.returncode,
        "stdout": out[:_SHELL_OUTPUT_CHARS],
        "stderr": err[:_SHELL_OUTPUT_CHARS],
        "stdout_truncated": len(out) > _SHELL_OUTPUT_CHARS,
        "stderr_truncated": len(err) > _SHELL_OUTPUT_CHARS,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "timed_out": False,
    }
