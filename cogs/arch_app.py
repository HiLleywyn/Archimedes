"""cogs/arch_app.py -- Archimedes application controls.

A focused, owner-only surface for the 3.0 application layer: Soul,
Heartbeat, Scheduler, MCP and the service chain. The Discord chat path
in ``cogs/chat`` is untouched -- this cog is the place an operator goes
to flip a switch, edit a soul preset, or inspect the heartbeat log
without leaving Discord.

Prefix commands only (``.arch app``); slash equivalents can be layered
on later through ``hybrid_command`` without disturbing this surface.
"""
from __future__ import annotations

import json
import logging

import discord
from discord.ext import commands

from arch.config import MCPServerSpec
from arch.scheduler import parse_oneshot_delay
from arch.soul import list_presets
from channels.session import channel_session_key, dm_session_key
from framework.context import ArchimedesContext
from framework.embed import card
from framework.middleware import require_owner
from framework.ui import C_INFO, C_NAVY, C_PURPLE, C_SUCCESS, C_WARNING, clip, fmt_ts

log = logging.getLogger(__name__)


class ArchApp(commands.Cog):
    """Operator controls for the Archimedes application layer."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ── command root ────────────────────────────────────────────────────────
    @commands.group(name="archapp", aliases=["app"], invoke_without_command=True)
    @require_owner
    async def archapp(self, ctx: ArchimedesContext, *, _rest: str = "") -> None:
        await self._send_help(ctx)

    async def _send_help(self, ctx: ArchimedesContext) -> None:
        p = ctx.prefix
        b = (
            card("Archimedes app",
                 description="Operator controls for the 3.0 application layer.",
                 color=C_PURPLE)
            .field(
                "Soul",
                f"`{p}app soul` show the active soul\n"
                f"`{p}app soul preset <name>` switch preset\n"
                f"`{p}app soul set <text>` write a custom soul",
                False,
            )
            .field(
                "Heartbeat",
                f"`{p}app heartbeat` show recent runs\n"
                f"`{p}app heartbeat run` fire one now",
                False,
            )
            .field(
                "Scheduler",
                f"`{p}app schedule list` your tasks\n"
                f"`{p}app schedule add <in 1h> <prompt>` queue a reminder\n"
                f"`{p}app schedule cron \"*/15 * * * *\" <prompt>` recurring",
                False,
            )
            .field(
                "MCP",
                f"`{p}app mcp` list connected servers\n"
                f"`{p}app mcp add <name> <url>` add HTTP server\n"
                f"`{p}app mcp remove <name>` drop a server",
                False,
            )
            .field(
                "Services",
                f"`{p}app services` show the model fallback chain",
                False,
            )
        )
        await ctx.reply(embed=b.build())

    def _agent(self):
        agent = getattr(self.bot, "arch", None)
        if agent is None:
            raise commands.CommandError("ArchAgent not running yet.")
        return agent

    # ── Soul ───────────────────────────────────────────────────────────────
    @archapp.group(name="soul", invoke_without_command=True)
    @require_owner
    async def soul(self, ctx: ArchimedesContext, *, _rest: str = "") -> None:
        agent = self._agent()
        rec = await agent.soul.get()
        b = (
            card("Soul",
                 description=clip(rec.prompt, 1500),
                 color=C_INFO)
            .field("Preset", rec.preset_name, True)
            .field("Updated", fmt_ts(rec.updated_at) if rec.updated_at else "-", True)
            .field("Presets available", ", ".join(list_presets()), False)
        )
        await ctx.reply(embed=b.build())

    @soul.command(name="preset")
    @require_owner
    async def soul_preset(self, ctx: ArchimedesContext, name: str) -> None:
        agent = self._agent()
        try:
            rec = await agent.soul.use_preset(name)
        except ValueError as exc:
            await ctx.reply_error(str(exc))
            return
        await ctx.reply(
            f"Soul switched to preset `{rec.preset_name}`."
        )

    @soul.command(name="set")
    @require_owner
    async def soul_set(self, ctx: ArchimedesContext, *, text: str) -> None:
        agent = self._agent()
        rec = await agent.soul.set(text, preset_name="custom")
        await ctx.reply(
            f"Soul updated. {len(rec.prompt)} chars saved as `custom`."
        )

    @soul.command(name="reset")
    @require_owner
    async def soul_reset(self, ctx: ArchimedesContext) -> None:
        agent = self._agent()
        await agent.soul.reset()
        await ctx.reply("Soul reset to default.")

    # ── Heartbeat ──────────────────────────────────────────────────────────
    @archapp.group(name="heartbeat", aliases=["hb"], invoke_without_command=True)
    @require_owner
    async def heartbeat(self, ctx: ArchimedesContext) -> None:
        agent = self._agent()
        recent = await agent.heartbeat_store.recent(limit=8)
        if not recent:
            await ctx.reply("No heartbeat runs recorded yet.")
            return
        lines = []
        for ran_at, status, detail in recent:
            badge = {
                "OK": "OK",
                "ACTED": "act",
                "FAIL": "FAIL",
            }.get(status, status)
            ts = ran_at.strftime("%Y-%m-%d %H:%M") if ran_at else "?"
            lines.append(f"`{badge:4}` {ts}  {clip(detail or '', 120)}")
        b = (
            card("Heartbeat", description="\n".join(lines), color=C_NAVY)
            .field("Enabled", str(agent.heartbeat is not None), True)
            .field("Interval", f"{agent.config.heartbeat.interval_minutes} min", True)
        )
        await ctx.reply(embed=b.build())

    @heartbeat.command(name="run")
    @require_owner
    async def heartbeat_run(self, ctx: ArchimedesContext) -> None:
        agent = self._agent()
        if agent.heartbeat is None:
            await ctx.reply_error(
                "Heartbeat is disabled. Set ARCHIMEDES_HEARTBEAT_ENABLED=1 "
                "and restart, or use a runtime toggle once one ships."
            )
            return
        result = await agent.heartbeat.trigger_once()
        await ctx.reply(
            f"Heartbeat: `{result.status}` in {result.duration_ms} ms"
            + (f"\n{clip(result.detail, 500)}" if result.detail else "")
        )

    # ── Scheduler ──────────────────────────────────────────────────────────
    @archapp.group(name="schedule", aliases=["sched"], invoke_without_command=True)
    @require_owner
    async def schedule(self, ctx: ArchimedesContext) -> None:
        await self._schedule_list(ctx)

    @schedule.command(name="list")
    @require_owner
    async def schedule_list(self, ctx: ArchimedesContext) -> None:
        await self._schedule_list(ctx)

    async def _schedule_list(self, ctx: ArchimedesContext) -> None:
        agent = self._agent()
        tasks = await agent.scheduler_store.list_for_owner(int(ctx.author.id))
        if not tasks:
            await ctx.reply("No scheduled tasks.")
            return
        lines = []
        for t in tasks:
            when = (
                t.cron_expr if t.kind == "cron"
                else (t.run_at.strftime("%Y-%m-%d %H:%M") if t.run_at else "?")
            )
            lines.append(
                f"`#{t.id:>4}` `{t.kind:7}` `{t.status:8}` {when}  "
                f"{clip((t.payload or {}).get('prompt', ''), 80)}"
            )
        await ctx.reply(
            embed=card("Scheduled tasks", description="\n".join(lines),
                       color=C_INFO).build(),
        )

    @schedule.command(name="add")
    @require_owner
    async def schedule_add(
        self, ctx: ArchimedesContext, when: str, *, prompt: str,
    ) -> None:
        agent = self._agent()
        run_at = parse_oneshot_delay(when)
        payload = self._payload_for(ctx, prompt)
        task_id = await agent.scheduler_store.add_oneshot(
            owner_id=int(ctx.author.id), run_at=run_at, payload=payload,
        )
        await ctx.reply(
            f"Task `#{task_id}` queued for {run_at.strftime('%Y-%m-%d %H:%M UTC')}."
        )

    @schedule.command(name="cron")
    @require_owner
    async def schedule_cron(
        self, ctx: ArchimedesContext, expression: str, *, prompt: str,
    ) -> None:
        agent = self._agent()
        payload = self._payload_for(ctx, prompt)
        task_id = await agent.scheduler_store.add_cron(
            owner_id=int(ctx.author.id), cron_expr=expression, payload=payload,
        )
        await ctx.reply(
            f"Cron task `#{task_id}` queued: `{expression}`."
        )

    @schedule.command(name="cancel")
    @require_owner
    async def schedule_cancel(self, ctx: ArchimedesContext, task_id: int) -> None:
        agent = self._agent()
        ok = await agent.scheduler_store.cancel(int(task_id))
        if ok:
            await ctx.reply(f"Task `#{task_id}` cancelled.")
        else:
            await ctx.reply_error(
                f"No active task `#{task_id}` to cancel.",
            )

    def _payload_for(self, ctx: ArchimedesContext, prompt: str) -> dict:
        guild = ctx.guild
        is_dm = guild is None
        if is_dm:
            session = dm_session_key("discord", ctx.author.id)
        else:
            session = channel_session_key("discord", ctx.channel.id)
        return {
            "prompt": prompt,
            "session_key": session,
            "transport": "discord",
            "user_id": int(ctx.author.id),
            "guild_id": int(guild.id) if guild else 0,
            "channel_id": int(ctx.channel.id),
        }

    # ── MCP ────────────────────────────────────────────────────────────────
    @archapp.group(name="mcp", invoke_without_command=True)
    @require_owner
    async def mcp(self, ctx: ArchimedesContext) -> None:
        agent = self._agent()
        servers = agent.mcp.servers()
        if not servers:
            await ctx.reply("No MCP servers connected.")
            return
        lines = []
        for t in servers:
            badge = "OK" if t.connected else "DOWN"
            line = f"`{badge:4}` `{t.spec.name}` {t.spec.transport}"
            if t.last_error and not t.connected:
                line += f"  -- {clip(t.last_error, 80)}"
            lines.append(line)
            for tool in t.tools[:6]:
                lines.append(f"     tool `{tool.name}`")
        await ctx.reply(
            embed=card("MCP servers", description="\n".join(lines),
                       color=C_INFO).build(),
        )

    @mcp.command(name="add")
    @require_owner
    async def mcp_add(self, ctx: ArchimedesContext, name: str, url: str) -> None:
        agent = self._agent()
        if not url.startswith(("http://", "https://")):
            await ctx.reply_error("URL must be an http or https endpoint.")
            return
        spec = MCPServerSpec(name=name, transport="http", url=url,
                             command="", args=())
        await agent.mcp.save(spec)
        transport = await agent.mcp.connect_one(spec)
        if not transport.connected:
            await ctx.reply_error(
                f"Saved `{name}` but the connect probe failed: "
                f"{clip(transport.last_error, 200)}"
            )
            return
        await ctx.reply(
            f"MCP server `{name}` connected ({len(transport.tools)} tool(s))."
        )

    @mcp.command(name="remove")
    @require_owner
    async def mcp_remove(self, ctx: ArchimedesContext, name: str) -> None:
        agent = self._agent()
        await agent.mcp.disconnect(name)
        await agent.mcp.forget(name)
        await ctx.reply(f"MCP server `{name}` removed.")

    # ── Services ───────────────────────────────────────────────────────────
    @archapp.command(name="services")
    @require_owner
    async def services_cmd(self, ctx: ArchimedesContext) -> None:
        agent = self._agent()
        health = agent.services.health()
        if not health:
            await ctx.reply("No services configured.")
            return
        lines = []
        for h in health:
            badge = "OK" if h.healthy else "DOWN"
            line = f"`{badge:4}` `{h.name}` failures={h.consecutive_failures}"
            if h.last_error:
                line += f"  -- {clip(h.last_error, 80)}"
            lines.append(line)
        await ctx.reply(
            embed=card("Service chain", description="\n".join(lines),
                       color=C_INFO).build(),
        )


async def setup(bot) -> None:
    await bot.add_cog(ArchApp(bot))
