"""tests/test_workspace.py -- offline tests for the sandboxed file workspace.

These exercise :mod:`ai.workspace` -- the per-server sandbox behind the
``files.*`` and ``shell.run`` agent tools -- and the registry wiring in
:mod:`ai.tools`. They need no Discord token, database or model: every test
points ``WORKSPACE_ROOT`` at a temporary directory.

The security-shaped tests (path traversal, per-server isolation, the shell
allowlist) are the point of this file: those are what make the tools safe to
expose to an untrusted Discord caller.
"""
from __future__ import annotations

import pytest

from ai import workspace
from ai.tools import ToolContext, build_default_registry
from config import Config


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """Point the workspace at a tmp dir with both tool groups enabled."""
    monkeypatch.setattr(Config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(Config, "WORKSPACE_ENABLED", True)
    monkeypatch.setattr(Config, "WORKSPACE_SHELL_ENABLED", True)
    return tmp_path


def _ctx(guild_id: int = 100, user_id: int = 1) -> ToolContext:
    return ToolContext(bot=None, db=None, user_id=user_id, guild_id=guild_id)


# ── file tools: the happy path ────────────────────────────────────────────────
def test_write_then_read_round_trip(ws) -> None:
    ctx = _ctx()
    written = workspace.write_file(ctx, "notes/todo.txt", "buy milk")
    assert written["created"] is True
    assert written["mode"] == "overwrite"
    assert written["bytes"] == len("buy milk")

    read = workspace.read_file(ctx, "notes/todo.txt")
    assert read["content"] == "buy milk"
    assert read["lines"] == 1
    assert read["path"] == "notes/todo.txt"


def test_overwrite_reports_the_file_as_not_created(ws) -> None:
    ctx = _ctx()
    workspace.write_file(ctx, "a.txt", "first")
    again = workspace.write_file(ctx, "a.txt", "second")
    assert again["created"] is False
    assert workspace.read_file(ctx, "a.txt")["content"] == "second"


def test_append_mode_extends_a_file(ws) -> None:
    ctx = _ctx()
    workspace.write_file(ctx, "log.txt", "line one\n")
    workspace.write_file(ctx, "log.txt", "line two\n", mode="append")
    assert workspace.read_file(ctx, "log.txt")["content"] == (
        "line one\nline two\n")


def test_list_reports_files_and_directories(ws) -> None:
    ctx = _ctx()
    workspace.write_file(ctx, "top.txt", "x")
    workspace.write_file(ctx, "sub/deep.txt", "y")
    listing = workspace.list_dir(ctx)
    by_name = {e["name"]: e for e in listing["entries"]}
    assert by_name["top.txt"]["type"] == "file"
    assert by_name["sub"]["type"] == "dir"
    assert listing["count"] == 2

    sub = workspace.list_dir(ctx, "sub")
    assert [e["name"] for e in sub["entries"]] == ["deep.txt"]


def test_grep_finds_matching_lines(ws) -> None:
    ctx = _ctx()
    workspace.write_file(ctx, "one.txt", "alpha\nbeta\ngamma\n")
    workspace.write_file(ctx, "two.txt", "BETA on its own line\n")

    hit = workspace.grep_files(ctx, "beta")
    assert hit["match_count"] == 1
    assert hit["matches"][0]["file"] == "one.txt"
    assert hit["matches"][0]["line"] == 2
    assert hit["files_searched"] == 2

    fold = workspace.grep_files(ctx, "beta", ignore_case=True)
    assert fold["match_count"] == 2


def test_grep_rejects_a_bad_regular_expression(ws) -> None:
    assert "error" in workspace.grep_files(_ctx(), "([unclosed")


def test_delete_removes_a_file(ws) -> None:
    ctx = _ctx()
    workspace.write_file(ctx, "trash.txt", "junk")
    assert workspace.delete_file(ctx, "trash.txt")["deleted"] is True
    assert "error" in workspace.read_file(ctx, "trash.txt")
    assert "error" in workspace.delete_file(ctx, "trash.txt")


def test_reading_a_missing_file_is_a_clean_error(ws) -> None:
    assert "error" in workspace.read_file(_ctx(), "nope.txt")


# ── the sandbox: confinement and isolation ────────────────────────────────────
@pytest.mark.parametrize("escape", [
    "../escape.txt",
    "../../etc/passwd",
    "/etc/passwd",
    "~/.bashrc",
    "sub/../../escape.txt",
])
def test_path_traversal_is_rejected(ws, escape) -> None:
    ctx = _ctx()
    assert "error" in workspace.read_file(ctx, escape)
    assert "error" in workspace.write_file(ctx, escape, "payload")
    assert "error" in workspace.delete_file(ctx, escape)
    # Nothing escaped the workspace root.
    assert list(ws.rglob("escape.txt")) == []


def test_workspaces_are_isolated_per_server(ws) -> None:
    server_a = _ctx(guild_id=100)
    server_b = _ctx(guild_id=200)
    workspace.write_file(server_a, "private.txt", "server A only")

    # Server B cannot see or read server A's file.
    assert workspace.list_dir(server_b)["entries"] == []
    assert "error" in workspace.read_file(server_b, "private.txt")
    # Server A still has it.
    assert workspace.read_file(server_a, "private.txt")["content"] == (
        "server A only")


def test_a_dm_caller_gets_a_per_user_workspace(ws) -> None:
    dm_user = _ctx(guild_id=0, user_id=42)
    other_user = _ctx(guild_id=0, user_id=99)
    workspace.write_file(dm_user, "mine.txt", "dm note")
    assert workspace.list_dir(other_user)["entries"] == []
    assert workspace.read_file(dm_user, "mine.txt")["content"] == "dm note"


# ── the sandbox: storage caps ─────────────────────────────────────────────────
def test_oversized_write_is_rejected(ws, monkeypatch) -> None:
    monkeypatch.setattr(Config, "WORKSPACE_MAX_FILE_KB", 1)
    assert "error" in workspace.write_file(_ctx(), "big.txt", "x" * 4000)


def test_workspace_quota_is_enforced(ws, monkeypatch) -> None:
    monkeypatch.setattr(Config, "WORKSPACE_QUOTA_KB", 1)
    ctx = _ctx()
    assert "error" not in workspace.write_file(ctx, "a.txt", "y" * 600)
    over = workspace.write_file(ctx, "b.txt", "z" * 600)
    assert "error" in over


def test_file_count_cap_is_enforced(ws, monkeypatch) -> None:
    monkeypatch.setattr(Config, "WORKSPACE_MAX_FILES", 1)
    ctx = _ctx()
    assert "error" not in workspace.write_file(ctx, "first.txt", "1")
    assert "error" in workspace.write_file(ctx, "second.txt", "2")
    # Overwriting an existing file is still allowed at the cap.
    assert "error" not in workspace.write_file(ctx, "first.txt", "updated")


# ── the allowlist shell ───────────────────────────────────────────────────────
async def test_shell_runs_an_allowlisted_command(ws) -> None:
    result = await workspace.run_shell(_ctx(), "echo hello-world")
    assert result["exit_code"] == 0
    assert "hello-world" in result["stdout"]
    assert result["timed_out"] is False


async def test_shell_runs_inside_the_workspace(ws) -> None:
    ctx = _ctx()
    workspace.write_file(ctx, "report.txt", "quarterly figures")
    listed = await workspace.run_shell(ctx, "ls")
    assert "report.txt" in listed["stdout"]
    catted = await workspace.run_shell(ctx, "cat report.txt")
    assert "quarterly figures" in catted["stdout"]


async def test_shell_rejects_a_command_off_the_allowlist(ws) -> None:
    for command in ("rm -rf .", "python -c pass", "curl http://x", "sh"):
        assert "error" in await workspace.run_shell(_ctx(), command)


async def test_shell_rejects_absolute_path_arguments(ws) -> None:
    result = await workspace.run_shell(_ctx(), "cat /etc/passwd")
    assert "error" in result
    assert "root:" not in result.get("stdout", "")


async def test_shell_rejects_parent_directory_arguments(ws) -> None:
    assert "error" in await workspace.run_shell(_ctx(), "cat ../../config.py")


async def test_shell_rejects_shell_operators(ws) -> None:
    for command in ("echo a; echo b", "echo a | wc -l",
                    "echo a > out.txt", "echo `whoami`", "cat $(ls)"):
        assert "error" in await workspace.run_shell(_ctx(), command)


async def test_shell_rejects_find_exec(ws) -> None:
    assert "error" in await workspace.run_shell(_ctx(), "find . -exec cat {} +")


async def test_shell_passes_no_secrets_in_the_environment(ws, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "super-secret-key")
    result = await workspace.run_shell(_ctx(), "echo done")
    # The command ran with a scrubbed environment, not the bot's.
    assert "super-secret-key" not in result.get("stdout", "")
    assert result["exit_code"] == 0


# ── registry wiring ───────────────────────────────────────────────────────────
def test_workspace_tools_are_registered_when_enabled(ws) -> None:
    names = {t.name for t in build_default_registry().all()}
    assert {"files.read", "files.write", "files.list", "files.grep",
            "files.delete", "shell.run"} <= names


def test_workspace_tools_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setattr(Config, "WORKSPACE_ENABLED", False)
    names = {t.name for t in build_default_registry().all()}
    assert not any(n.startswith(("files.", "shell.")) for n in names)


def test_shell_tool_can_be_disabled_alone(monkeypatch) -> None:
    monkeypatch.setattr(Config, "WORKSPACE_ENABLED", True)
    monkeypatch.setattr(Config, "WORKSPACE_SHELL_ENABLED", False)
    names = {t.name for t in build_default_registry().all()}
    assert "files.read" in names
    assert "shell.run" not in names


async def test_files_tools_round_trip_through_the_registry(ws) -> None:
    reg = build_default_registry()
    ctx = _ctx()
    ctx.registry = reg
    write = await reg.run("files.write",
                          {"path": "via.txt", "content": "registry path"}, ctx)
    assert write["created"] is True
    read = await reg.run("files.read", {"path": "via.txt"}, ctx)
    assert read["content"] == "registry path"
