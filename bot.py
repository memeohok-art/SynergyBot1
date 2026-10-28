#!/usr/bin/env python3
"""
THEMASTEROOGWAY BOT
===================
A single-file Discord bot: moderation, utility and fun commands.

QUICK START
-----------
1. pip install -r requirements.txt
2. Paste your bot token below (or set the DISCORD_TOKEN environment variable)
3. python bot.py

Developer Portal checklist:
  * Bot -> Privileged Gateway Intents -> MESSAGE CONTENT INTENT : ON
  * OAuth2 -> scopes: bot + applications.commands
  * Permissions: Administrator (simplest) or at minimum
    Ban Members, Kick Members, Moderate Members, Manage Messages,
    Manage Roles, Manage Channels, Manage Nicknames, Read/Send Messages,
    Add Reactions, Embed Links
"""

from __future__ import annotations

import asyncio
import ast
import base64
import hashlib
import json
import operator
import os
import random
import re
import secrets
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

import discord
from discord.ext import commands

# ===========================================================================
# CONFIG
# ===========================================================================

def _load_dotenv(path: str = ".env") -> None:
    """Read a simple .env file sitting next to bot.py (no extra packages)."""
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except Exception as exc:
        print(f"Could not read {path}: {exc}")


_load_dotenv()

# Paste your token between the quotes when running locally.
# Leave it as-is if you set the DISCORD_TOKEN environment variable instead.
TOKEN: str = os.getenv("DISCORD_TOKEN", "PASTE_YOUR_TOKEN_HERE").strip()

PREFIX: str = os.getenv("PREFIX", "!") or "!"

# The YouTube handle this bot watches for (see !notify)
WATCH_HANDLE: str = os.getenv("WATCH_HANDLE", "themasteroogway_")

# Who gets DMed by !notify. Leave 0 to DM the bot's own application owner.
OWNER_ID: int = int(os.getenv("OWNER_ID", "0") or 0)

# Optional channel id where failed DMs / mod actions are logged. 0 = off.
LOG_CHANNEL_ID: int = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)

# How long one person must wait between !notify DMs (anti-spam), seconds.
NOTIFY_COOLDOWN: int = int(os.getenv("COOLDOWN_SECONDS", "20") or 20)

# Role that can see and answer support tickets. 0 = staff with Manage Channels only.
SUPPORT_ROLE_ID: int = int(os.getenv("SUPPORT_ROLE_ID", "0") or 0)

# Category new ticket channels are created in. 0 = server root.
TICKET_CATEGORY_ID: int = int(os.getenv("TICKET_CATEGORY_ID", "0") or 0)

# Where warnings + giveaways are saved so they survive a restart.
DATA_FILE: str = os.getenv("DATA_FILE", "botdata.json")

# How often pending changes are written to disk (seconds).
AUTOSAVE_SECONDS: float = float(os.getenv("AUTOSAVE_SECONDS", "3") or 3)

COLOUR_OK = 0x2ECC71
COLOUR_ERR = 0xE74C3C
COLOUR_INFO = 0x3498DB
COLOUR_FUN = 0xFF0033


# ===========================================================================
# BOT SETUP
# ===========================================================================

def _members_intent_allowed() -> bool:
    """Ask Discord whether the Server Members Intent is switched on in the portal.

    Checking the app flags first means we never crash with
    PrivilegedIntentsRequired if the toggle is off.
    """
    override = (os.getenv("MEMBERS_INTENT") or "").strip().lower()
    if override in ("0", "false", "no", "off"):
        return False
    if override in ("1", "true", "yes", "on"):
        return True
    try:
        import json as _json
        import urllib.request

        req = urllib.request.Request(
            "https://discord.com/api/v10/oauth2/applications/@me",
            headers={"Authorization": f"Bot {TOKEN}", "User-Agent": "DiscordBot (synergy, 1.0)"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            flags = int(_json.load(resp).get("flags", 0))
        return bool(flags & ((1 << 14) | (1 << 15)))  # GUILD_MEMBERS / _LIMITED
    except Exception as exc:
        print(f"Could not check the members intent ({exc}) - assuming it is off.")
        return False


intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
# Must be set before commands.Bot() is built: Client.intents returns a copy,
# so assigning to bot.intents.members later has no effect.
intents.members = _members_intent_allowed()
# If the portal toggle is off, this resolves to False and the bot still starts
# (requesting an ungranted privileged intent would kill the connection).

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None,
    case_insensitive=True,
    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False),
    activity=discord.Activity(type=discord.ActivityType.watching, name=WATCH_HANDLE),
)


# ===========================================================================
# SMALL HELPERS
# ===========================================================================

def ok(desc: str, title: str = "Done") -> discord.Embed:
    return discord.Embed(title=title, description=desc, colour=COLOUR_OK)


def err(desc: str, title: str = "Nope") -> discord.Embed:
    return discord.Embed(title=title, description=desc, colour=COLOUR_ERR)


def info(desc: str, title: str = "") -> discord.Embed:
    return discord.Embed(title=title or None, description=desc, colour=COLOUR_INFO)


def fun(desc: str, title: str = "") -> discord.Embed:
    return discord.Embed(title=title or None, description=desc, colour=COLOUR_FUN)


def seed(*parts: object) -> int:
    """Deterministic pseudo-random number (same inputs -> same result)."""
    raw = "|".join(str(p) for p in parts)
    return int(hashlib.md5(raw.encode("utf-8")).hexdigest()[:8], 16)


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    parts = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{label}")
            seconds %= size
    return " ".join(parts) or "0s"


DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
DUR_RUNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> int:
    """'10m' -> 600. Accepts 30s 5m 2h 1d 1w and combos like 1h30m."""
    total = 0
    for amount, unit in DURATION_RE.findall(text or ""):
        total += int(amount) * DUR_RUNITS[unit.lower()]
    return total


def moderator_block(ctx: commands.Context, member: discord.Member) -> Optional[str]:
    """Return a refusal message if the moderator can't touch this member."""
    if member.id == ctx.guild.owner_id:
        return "That's the server owner — Discord doesn't allow it."
    if member.id == ctx.author.id:
        return "You can't target yourself."
    if ctx.me and member.id == ctx.me.id:
        return "You can't target me."
    if ctx.me and member.top_role >= ctx.me.top_role:
        return f"My highest role is below {member.mention}'s — move my role up first."
    if ctx.author.id != ctx.guild.owner_id and member.top_role >= ctx.author.top_role:
        return f"Your highest role is below {member.mention}'s."
    return None


async def log_action(guild: discord.Guild, text: str) -> None:
    if not LOG_CHANNEL_ID:
        return
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        return
    try:
        await channel.send(embed=info(text, "Mod log"))
    except Exception:
        pass


async def get_owner() -> Optional[discord.User]:
    if OWNER_ID:
        user = bot.get_user(OWNER_ID)
        if user:
            return user
        try:
            return await bot.fetch_user(OWNER_ID)
        except Exception:
            return None
    try:
        app = await bot.application_info()
        return app.owner
    except Exception:
        return None


# ===========================================================================
# 1. CORE
# ===========================================================================

@bot.hybrid_command(name="ping")
async def ping(ctx: commands.Context) -> None:
    """Check the bot's latency."""
    start = time.perf_counter()
    msg = await ctx.send(embed=info("Pinging..."))
    rtt = (time.perf_counter() - start) * 1000
    # bot.latency is NaN until the gateway has completed a heartbeat
    gw = bot.latency * 1000
    gw = 0 if gw != gw else round(gw)
    await msg.edit(
        embed=info(
            f"**Gateway:** `{gw} ms`\n"
            f"**Round trip:** `{round(rtt)} ms`",
            "🏓 Pong",
        )
    )


@bot.hybrid_command(name="help")
async def help_cmd(ctx: commands.Context, *, command: Optional[str] = None) -> None:
    """Show every command, or details for one command."""
    if command:
        cmd = bot.get_command(command.lstrip(PREFIX))
        if cmd is None:
            await ctx.send(embed=err(f"No command called `{command}`."))
            return
        embed = discord.Embed(
            title=f"{PREFIX}{cmd.qualified_name}",
            description=cmd.help or "No description.",
            colour=COLOUR_INFO,
        )
        if cmd.signature:
            embed.add_field(name="Usage", value=f"`{PREFIX}{cmd.qualified_name} {cmd.signature}`", inline=False)
        aliases = ", ".join(f"`{a}`" for a in cmd.aliases)
        if aliases:
            embed.add_field(name="Aliases", value=aliases, inline=False)
        await ctx.send(embed=embed)
        return

    embed = discord.Embed(
        title="📖 Commands",
        description=f"Prefix: `{PREFIX}`  •  `{len(bot.commands)}` commands  •  `{PREFIX}help <command>` for details",
        colour=COLOUR_INFO,
    )
    for category, names in CATEGORIES.items():
        listed = []
        for name in names:
            cmd = bot.get_command(name)
            if cmd is not None:
                listed.append(f"`{name}`")
        if listed:
            embed.add_field(name=category, value=" ".join(listed), inline=False)
    embed.set_footer(
        text=f"Every command works as a slash command too  •  requested by {ctx.author}",
        icon_url=ctx.author.display_avatar.url,
    )
    await ctx.send(embed=embed)


@bot.hybrid_command(name="botinfo", aliases=["about"])
async def botinfo(ctx: commands.Context) -> None:
    """Show information about this bot."""
    embed = discord.Embed(title="Bot info", colour=COLOUR_INFO)
    embed.add_field(name="Servers", value=str(len(bot.guilds)))
    embed.add_field(name="Users", value=str(sum(g.member_count or 0 for g in bot.guilds)))
    embed.add_field(name="Commands", value=str(len(bot.commands)))
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)} ms")
    embed.add_field(name="discord.py", value=discord.__version__)
    embed.add_field(name="Python", value=f"{sys.version_info.major}.{sys.version_info.minor}")
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    await ctx.send(embed=embed)


START_TIME = time.time()


@bot.hybrid_command(name="uptime")
async def uptime(ctx: commands.Context) -> None:
    """How long the bot has been online."""
    await ctx.send(embed=info(f"Online for **{fmt_duration(time.time() - START_TIME)}**", "⏱ Uptime"))


@bot.hybrid_command(name="invite")
async def invite(ctx: commands.Context) -> None:
    """Get a link to add this bot to another server."""
    perms = discord.Permissions(administrator=True)
    url = discord.utils.oauth_url(bot.user.id, permissions=perms, scopes=("bot", "applications.commands"))
    await ctx.send(embed=info(f"[Click here to invite me]({url})", "➕ Invite"))


async def live_guild_counts(guild: discord.Guild) -> tuple[Optional[int], Optional[int]]:
    """Fetch fresh member/online counts straight from the API.

    The gateway's cached `member_count` goes stale because join/leave events
    need the Server Members Intent, which this bot does not request. Asking the
    REST API every time keeps the number honest.
    """
    try:
        data = await bot.http.get_guild(guild.id, with_counts=True)
        return data.get("approximate_member_count"), data.get("approximate_presence_count")
    except Exception:
        return guild.member_count, None


@bot.hybrid_command(name="membercount", aliases=["mc"])
@commands.guild_only()
async def membercount(ctx: commands.Context) -> None:
    """Show how many people are in the server."""
    g = ctx.guild
    total, online = await live_guild_counts(g)

    lines = [f"**Total:** {total}"]
    if online is not None:
        lines.append(f"**Online now:** {online}")

    cached = g.members
    if cached:
        humans = sum(1 for m in cached if not m.bot)
        lines.append(f"**Humans:** {humans}")
        lines.append(f"**Bots:** {len(cached) - humans}")
    else:
        lines.append(
            "\n_Human/bot split needs the **Server Members Intent**: "
            "Developer Portal \u2192 Bot \u2192 Privileged Gateway Intents._"
        )

    await ctx.send(embed=info("\n".join(lines), f"👥 {g.name}"))


# ===========================================================================
# 2. MODERATION
# ===========================================================================

@bot.hybrid_command(name="ban")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def ban(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason given") -> None:
    """Ban a member. Usage: !ban @user [reason]"""
    block = moderator_block(ctx, member)
    if block:
        await ctx.send(embed=err(block))
        return
    await member.ban(reason=f"{ctx.author} ({ctx.author.id}): {reason}", delete_message_days=0)
    await ctx.send(embed=ok(f"🔨 Banned **{member}**\n**Reason:** {reason}"))
    await log_action(ctx.guild, f"{ctx.author.mention} banned {member} — {reason}")


@bot.hybrid_command(name="unban")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def unban(ctx: commands.Context, user_id: int, *, reason: str = "No reason given") -> None:
    """Unban a user by ID. Usage: !unban 123456789012345678"""
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
    except discord.NotFound:
        await ctx.send(embed=err("That user isn't banned (or the ID is wrong)."))
        return
    await ctx.send(embed=ok(f"✅ Unbanned **{user}**"))
    await log_action(ctx.guild, f"{ctx.author.mention} unbanned {user} — {reason}")


@bot.hybrid_command(name="kick")
@commands.guild_only()
@commands.has_permissions(kick_members=True)
@commands.bot_has_permissions(kick_members=True)
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason given") -> None:
    """Kick a member. Usage: !kick @user [reason]"""
    block = moderator_block(ctx, member)
    if block:
        await ctx.send(embed=err(block))
        return
    await member.kick(reason=f"{ctx.author}: {reason}")
    await ctx.send(embed=ok(f"👢 Kicked **{member}**\n**Reason:** {reason}"))
    await log_action(ctx.guild, f"{ctx.author.mention} kicked {member} — {reason}")


@bot.hybrid_command(name="softban")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def softban(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason given") -> None:
    """Ban then instantly unban, to delete their recent messages."""
    block = moderator_block(ctx, member)
    if block:
        await ctx.send(embed=err(block))
        return
    await member.ban(reason=f"softban by {ctx.author}: {reason}", delete_message_days=7)
    await ctx.guild.unban(discord.Object(id=member.id), reason="softban - auto unban")
    await ctx.send(embed=ok(f"🧹 Softbanned **{member}** (messages cleared, they can rejoin)"))


@bot.hybrid_command(name="timeout", aliases=["mute"])
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def timeout(ctx: commands.Context, member: discord.Member, duration: str = "10m", *, reason: str = "No reason given") -> None:
    """Timeout a member. Usage: !timeout @user 10m [reason]"""
    block = moderator_block(ctx, member)
    if block:
        await ctx.send(embed=err(block))
        return
    seconds = parse_duration(duration)
    if seconds < 1:
        await ctx.send(embed=err("Give a duration like `30s`, `10m`, `2h` or `1d`."))
        return
    if seconds > 28 * 86400:
        await ctx.send(embed=err("Discord caps timeouts at 28 days."))
        return
    await member.timeout(timedelta(seconds=seconds), reason=f"{ctx.author}: {reason}")
    await ctx.send(embed=ok(f"🔇 **{member}** is timed out for **{fmt_duration(seconds)}**\n**Reason:** {reason}"))


@bot.hybrid_command(name="untimeout", aliases=["unmute"])
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def untimeout(ctx: commands.Context, member: discord.Member) -> None:
    """Remove a timeout from a member."""
    await member.timeout(None, reason=f"untimeout by {ctx.author}")
    await ctx.send(embed=ok(f"🔊 **{member}** can talk again."))


WARNINGS: dict[int, list[tuple[str, str]]] = {}


@bot.hybrid_command(name="warn")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def warn(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason given") -> None:
    """Warn a member (stored in memory). Usage: !warn @user [reason]"""
    block = moderator_block(ctx, member)
    if block:
        await ctx.send(embed=err(block))
        return
    WARNINGS.setdefault(member.id, []).append((str(ctx.author), reason))
    save_data()  # persisted immediately - warnings must never be lost
    total = len(WARNINGS[member.id])
    await ctx.send(embed=ok(f"⚠️ **{member}** warned ({total} total)\n**Reason:** {reason}"))
    try:
        await member.send(embed=info(f"You were warned in **{ctx.guild.name}**\n**Reason:** {reason}", "⚠️ Warning"))
    except discord.Forbidden:
        pass


@bot.hybrid_command(name="warnings", aliases=["warns"])
@commands.guild_only()
async def warnings(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    """List a member's warnings."""
    target = member or ctx.author
    entries = WARNINGS.get(target.id, [])
    if not entries:
        await ctx.send(embed=info(f"**{target}** has no warnings. 🎉"))
        return
    lines = [f"`{i}.` {reason} — *by {who}*" for i, (who, reason) in enumerate(entries, 1)]
    await ctx.send(embed=info("\n".join(lines[:20]), f"⚠️ {target} — {len(entries)} warning(s)"))


@bot.hybrid_command(name="clearwarnings", aliases=["clearwarns"])
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def clearwarnings(ctx: commands.Context, member: discord.Member) -> None:
    """Clear all warnings for a member (admin only)."""
    WARNINGS.pop(member.id, None)
    save_data()
    await ctx.send(embed=ok(f"Cleared warnings for **{member}**."))


@bot.hybrid_command(name="purge", aliases=["clear"])
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True)
async def purge(ctx: commands.Context, amount: int = 10) -> None:
    """Delete recent messages. Usage: !purge 25 (max 200)"""
    amount = max(1, min(amount, 200))
    deleted = await ctx.channel.purge(limit=amount + 1)
    await ctx.send(embed=ok(f"🗑 Deleted **{len(deleted) - 1}** message(s)."), delete_after=5)


@bot.hybrid_command(name="slowmode")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def slowmode(ctx: commands.Context, duration: str = "0s") -> None:
    """Set channel slowmode. Usage: !slowmode 10s (0 to disable)"""
    seconds = parse_duration(duration) if duration not in ("0", "off") else 0
    if seconds > 21600:
        await ctx.send(embed=err("Discord caps slowmode at 6 hours."))
        return
    await ctx.channel.edit(slowmode_delay=seconds)
    await ctx.send(embed=ok(f"🐌 Slowmode set to **{fmt_duration(seconds)}**." if seconds else "🐇 Slowmode disabled."))


@bot.hybrid_command(name="lock")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def lock(ctx: commands.Context) -> None:
    """Stop @everyone from talking in this channel."""
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send(embed=ok("🔒 Channel locked."))


@bot.hybrid_command(name="unlock")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def unlock(ctx: commands.Context) -> None:
    """Let @everyone talk in this channel again."""
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send(embed=ok("🔓 Channel unlocked."))


@bot.hybrid_command(name="nuke")
@commands.guild_only()
@commands.has_permissions(administrator=True)
@commands.bot_has_permissions(manage_channels=True)
async def nuke(ctx: commands.Context) -> None:
    """Clone this channel and delete the old one (wipes all messages)."""
    channel = ctx.channel
    position = channel.position
    clone = await channel.clone(reason=f"Nuke by {ctx.author}")
    await channel.delete(reason=f"Nuke by {ctx.author}")
    await clone.edit(position=position)
    await clone.send(embed=ok("💥 Channel nuked. Fresh start."))


@bot.hybrid_command(name="addrole", aliases=["giverole"])
@commands.guild_only()
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def addrole(ctx: commands.Context, member: discord.Member, *, role: discord.Role) -> None:
    """Give a role to a member."""
    if role >= ctx.guild.me.top_role:
        await ctx.send(embed=err("That role is above my highest role."))
        return
    await member.add_roles(role, reason=f"addrole by {ctx.author}")
    await ctx.send(embed=ok(f"✅ Gave {role.mention} to **{member}**."))


@bot.hybrid_command(name="removerole", aliases=["takerole"])
@commands.guild_only()
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def removerole(ctx: commands.Context, member: discord.Member, *, role: discord.Role) -> None:
    """Remove a role from a member."""
    if role >= ctx.guild.me.top_role:
        await ctx.send(embed=err("That role is above my highest role."))
        return
    await member.remove_roles(role, reason=f"removerole by {ctx.author}")
    await ctx.send(embed=ok(f"➖ Removed {role.mention} from **{member}**."))


@bot.hybrid_command(name="nick")
@commands.guild_only()
@commands.has_permissions(manage_nicknames=True)
@commands.bot_has_permissions(manage_nicknames=True)
async def nick(ctx: commands.Context, member: discord.Member, *, nickname: Optional[str] = None) -> None:
    """Change a nickname. Leave blank to reset. Usage: !nick @user NewName"""
    await member.edit(nick=nickname, reason=f"nick by {ctx.author}")
    await ctx.send(embed=ok(f"✏️ Nickname set to **{nickname or member.name}**."))


@bot.hybrid_command(name="say")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def say(ctx: commands.Context, *, text: str) -> None:
    """Make the bot repeat something."""
    if ctx.message is not None:
        await ctx.message.delete()
    await ctx.send(text)


@bot.hybrid_command(name="announce")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def announce(ctx: commands.Context, channel: discord.TextChannel, *, text: str) -> None:
    """Post a fancy announcement. Usage: !announce #general Hello everyone"""
    embed = discord.Embed(title="📢 Announcement", description=text, colour=COLOUR_INFO)
    embed.set_footer(text=f"From {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await channel.send(embed=embed)
    await ctx.send(embed=ok(f"Announcement posted in {channel.mention}."))


# ===========================================================================
# 3. INFO / USER COMMANDS
# ===========================================================================

@bot.hybrid_command(name="userinfo", aliases=["whois"])
@commands.guild_only()
async def userinfo(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    """Show information about a member."""
    m = member or ctx.author
    roles = [r.mention for r in getattr(m, "roles", []) if r.name != "@everyone"]
    embed = discord.Embed(title=f"👤 {m}", colour=m.colour if getattr(m, "colour", None) else COLOUR_INFO)
    embed.set_thumbnail(url=m.display_avatar.url)
    embed.add_field(name="ID", value=f"`{m.id}`")
    embed.add_field(name="Bot", value="Yes" if m.bot else "No")
    embed.add_field(name="Created", value=discord.utils.format_dt(m.created_at, "R"))
    if getattr(m, "joined_at", None):
        embed.add_field(name="Joined", value=discord.utils.format_dt(m.joined_at, "R"))
    embed.add_field(name="Top role", value=getattr(m, "top_role", "—").mention if getattr(m, "top_role", None) else "—")
    if roles:
        embed.add_field(name=f"Roles ({len(roles)})", value=", ".join(roles[:20])[:1024], inline=False)
    await ctx.send(embed=embed)


@bot.hybrid_command(name="serverinfo")
@commands.guild_only()
async def serverinfo(ctx: commands.Context) -> None:
    """Show information about this server."""
    g = ctx.guild
    embed = discord.Embed(title=f"🏰 {g.name}", colour=COLOUR_INFO)
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="Owner", value=f"<@{g.owner_id}>")
    total, online = await live_guild_counts(g)
    embed.add_field(name="Members", value=str(total))
    embed.add_field(name="Online", value=str(online) if online is not None else "—")
    embed.add_field(name="Channels", value=str(len(g.channels)))
    embed.add_field(name="Roles", value=str(len(g.roles)))
    embed.add_field(name="Boosts", value=f"{g.premium_subscription_count} (level {g.premium_tier})")
    embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "R"))
    embed.set_footer(text=f"ID: {g.id}")
    await ctx.send(embed=embed)


@bot.hybrid_command(name="avatar", aliases=["av", "pfp"])
async def avatar(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    """Show someone's avatar."""
    m = member or ctx.author
    embed = discord.Embed(title=f"🖼 {m.display_name}'s avatar", colour=COLOUR_INFO)
    embed.set_image(url=m.display_avatar.with_size(1024).url)
    await ctx.send(embed=embed)


@bot.hybrid_command(name="servericon", aliases=["icon"])
@commands.guild_only()
async def servericon(ctx: commands.Context) -> None:
    """Show the server icon."""
    if not ctx.guild.icon:
        await ctx.send(embed=err("This server has no icon."))
        return
    embed = discord.Embed(title=f"🖼 {ctx.guild.name}", colour=COLOUR_INFO)
    embed.set_image(url=ctx.guild.icon.with_size(1024).url)
    await ctx.send(embed=embed)


@bot.hybrid_command(name="roles")
@commands.guild_only()
async def roles(ctx: commands.Context) -> None:
    """List every role in the server."""
    names = [r.mention for r in reversed(ctx.guild.roles) if r.name != "@everyone"]
    chunks, current = [], ""
    for name in names:
        if len(current) + len(name) > 1000:
            chunks.append(current)
            current = ""
        current += name + " "
    if current:
        chunks.append(current)
    embed = discord.Embed(title=f"🎭 Roles ({len(names)})", description=chunks[0] if chunks else "None", colour=COLOUR_INFO)
    await ctx.send(embed=embed)


@bot.hybrid_command(name="channelinfo", aliases=["cinfo"])
@commands.guild_only()
async def channelinfo(ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
    """Show information about a channel."""
    c = channel or ctx.channel
    embed = discord.Embed(title=f"# {c.name}", colour=COLOUR_INFO)
    embed.add_field(name="ID", value=f"`{c.id}`")
    embed.add_field(name="Type", value=str(c.type))
    embed.add_field(name="Created", value=discord.utils.format_dt(c.created_at, "R"))
    embed.add_field(name="Slowmode", value=f"{c.slowmode_delay}s")
    embed.add_field(name="NSFW", value="Yes" if c.nsfw else "No")
    embed.add_field(name="Topic", value=(c.topic or "—")[:200], inline=False)
    await ctx.send(embed=embed)


SNIPES: dict[int, tuple[str, str]] = {}


@bot.event
async def on_message_delete(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return
    SNIPES[message.channel.id] = (str(message.author), message.content or "[attachment]")


@bot.hybrid_command(name="snipe")
@commands.guild_only()
async def snipe(ctx: commands.Context) -> None:
    """Show the most recently deleted message in this channel."""
    data = SNIPES.get(ctx.channel.id)
    if not data:
        await ctx.send(embed=err("Nothing to snipe."))
        return
    author, content = data
    embed = discord.Embed(description=content[:1900], colour=COLOUR_INFO)
    embed.set_author(name=f"{author} said:")
    await ctx.send(embed=embed)


AFK: dict[int, str] = {}


@bot.hybrid_command(name="afk")
async def afk(ctx: commands.Context, *, reason: str = "AFK") -> None:
    """Set an AFK status. Usage: !afk grabbing food"""
    AFK[ctx.author.id] = reason
    await ctx.send(embed=ok(f"💤 **{ctx.author.display_name}** is now AFK: {reason}"))


@bot.hybrid_command(name="remind", aliases=["reminder"])
async def remind(ctx: commands.Context, duration: str, *, text: str = "something") -> None:
    """Set a reminder. Usage: !remind 10m check the oven"""
    seconds = parse_duration(duration)
    if seconds < 1:
        await ctx.send(embed=err("Give a duration like `30s`, `10m`, `2h`, `1d`."))
        return
    if seconds > 86400:
        await ctx.send(embed=err("Keep reminders under 24 hours."))
        return
    await ctx.send(embed=ok(f"⏰ I'll remind you in **{fmt_duration(seconds)}**."))

    async def waiter() -> None:
        await asyncio.sleep(seconds)
        try:
            await ctx.reply(f"⏰ {ctx.author.mention} reminder: **{text}**", mention_author=True)
        except Exception:
            pass

    asyncio.create_task(waiter())


@bot.hybrid_command(name="poll")
@commands.guild_only()
async def poll(ctx: commands.Context, *, text: str) -> None:
    """Create a reaction poll. Usage: !poll Pizza | Sushi | Tacos"""
    parts = [p.strip() for p in text.split("|") if p.strip()]
    if len(parts) < 2:
        await ctx.send(embed=err("Use `|` between options: `!poll Question | A | B`"))
        return
    question, options = parts[0], parts[1:6]
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    lines = [f"{emojis[i]} {opt}" for i, opt in enumerate(options)]
    embed = discord.Embed(title=f"📊 {question}", description="\n".join(lines), colour=COLOUR_INFO)
    embed.set_footer(text=f"Poll by {ctx.author}")
    msg = await ctx.send(embed=embed)
    for i in range(len(options)):
        await msg.add_reaction(emojis[i])


@bot.hybrid_command(name="math", aliases=["calc"])
async def math(ctx: commands.Context, *, expression: str) -> None:
    """Do simple math. Usage: !math (12+8)*3"""
    ops = {
        ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Num):
            return node.n  # type: ignore[attr-defined]
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 64 or abs(left) > 1e6):
                raise ValueError("that exponent is too big")
            return ops[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.operand))
        raise ValueError("unsupported expression")

    try:
        result = ev(ast.parse(expression, mode="eval").body)
    except Exception:
        await ctx.send(embed=err("I can't evaluate that. Try something like `(12+8)*3`."))
        return
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    await ctx.send(embed=info(f"`{expression}` = **{result}**", "🧮 Result"))


@bot.hybrid_command(name="b64")
async def b64(ctx: commands.Context, mode: str, *, text: str) -> None:
    """Base64 encode or decode. Usage: !b64 encode hello  /  !b64 decode aGVsbG8="""
    if mode.lower().startswith("e"):
        out = base64.b64encode(text.encode()).decode()
    elif mode.lower().startswith("d"):
        try:
            out = base64.b64decode(text, validate=True).decode()
        except Exception:
            await ctx.send(embed=err("That isn't valid base64."))
            return
    else:
        await ctx.send(embed=err("Use `encode` or `decode`."))
        return
    await ctx.send(embed=info(f"```{out[:1800]}```", "🔐 Base64"))


@bot.hybrid_command(name="password")
async def password(ctx: commands.Context, length: int = 16) -> None:
    """Generate a random password. Usage: !password 20"""
    length = max(6, min(length, 128))
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%^&*-_=+"
    pwd = "".join(secrets.choice(alphabet) for _ in range(length))
    await ctx.send(embed=info(f"```{pwd}```\nCopy it somewhere safe — this message is public.", "🔑 Password"))


@bot.hybrid_command(name="randomnumber", aliases=["randnum"])
async def randomnumber(ctx: commands.Context, low: int = 1, high: int = 100) -> None:
    """Random number in a range. Usage: !randomnumber 1 10"""
    if low > high:
        low, high = high, low
    await ctx.send(embed=fun(f"🎲 **{random.randint(low, high)}** (from {low}–{high})"))


@bot.hybrid_command(name="uuidgen", aliases=["uuid"])
async def uuidgen(ctx: commands.Context) -> None:
    """Generate a random UUID v4."""
    import uuid as _uuid
    await ctx.send(embed=info(f"`{_uuid.uuid4()}`", "🔖 UUID"))


@bot.hybrid_command(name="lmgtfy")
async def lmgtfy(ctx: commands.Context, *, query: str) -> None:
    """Let me Google that for you."""
    from urllib.parse import quote_plus
    url = f"https://www.google.com/search?q={quote_plus(query)}"
    await ctx.send(embed=info(f"[{query}]({url})", "🔍 Search"))


@bot.hybrid_command(name="timestamp")
async def timestamp(ctx: commands.Context, duration: str = "1h") -> None:
    """Get a Discord timestamp for a time from now. Usage: !timestamp 2d"""
    seconds = parse_duration(duration) or 3600
    moment = discord.utils.utcnow() + timedelta(seconds=seconds)
    await ctx.send(embed=info(f"`{discord.utils.format_dt(moment, 'f')}`\n"
                              f"`<t:{int(moment.timestamp())}:R>`", "🕐 Timestamp"))


# ===========================================================================
# 4. FUN
# ===========================================================================

@bot.hybrid_command(name="8ball")
async def eightball(ctx: commands.Context, *, question: str) -> None:
    """Ask the magic 8-ball a question."""
    answers = [
        "It is certain.", "Without a doubt.", "Yes, definitely.", "Most likely.",
        "Yes.", "Signs point to yes.", "Reply hazy, try again.", "Ask again later.",
        "Better not tell you now.", "Cannot predict now.", "Don't count on it.",
        "My reply is no.", "Very doubtful.", "Absolutely not.", "Outlook not so good.",
    ]
    embed = discord.Embed(title="🎱 " + question[:200], description=f"**{random.choice(answers)}**", colour=COLOUR_FUN)
    await ctx.send(embed=embed)


@bot.hybrid_command(name="roll", aliases=["dice"])
async def roll(ctx: commands.Context, dice: str = "1d6") -> None:
    """Roll dice. Usage: !roll 2d20"""
    match = re.fullmatch(r"(\d{1,2})d(\d{1,3})", dice.lower().replace(" ", ""))
    if not match:
        await ctx.send(embed=err("Format is `NdM`, e.g. `1d6` or `2d20`."))
        return
    count, sides = int(match.group(1)), int(match.group(2))
    if not 1 <= count <= 20 or not 2 <= sides <= 1000:
        await ctx.send(embed=err("Keep it to 1–20 dice with 2–1000 sides."))
        return
    rolls = [random.randint(1, sides) for _ in range(count)]
    total = sum(rolls)
    desc = f"**{' + '.join(map(str, rolls))}** = **{total}**" if count > 1 else f"**{total}**"
    await ctx.send(embed=fun(desc, f"🎲 {dice}"))


@bot.hybrid_command(name="coinflip", aliases=["flip"])
async def coinflip(ctx: commands.Context) -> None:
    """Flip a coin."""
    await ctx.send(embed=fun(f"🪙 **{random.choice(['Heads', 'Tails'])}**"))


@bot.hybrid_command(name="choose", aliases=["pick"])
async def choose(ctx: commands.Context, *, options: str) -> None:
    """Pick between options. Usage: !choose pizza | burger | salad"""
    parts = [p.strip() for p in re.split(r"[|,]", options) if p.strip()]
    if len(parts) < 2:
        await ctx.send(embed=err("Separate options with `|`: `!choose A | B`"))
        return
    await ctx.send(embed=fun(f"👉 **{random.choice(parts)}**", "I choose..."))


@bot.hybrid_command(name="rps")
async def rps(ctx: commands.Context, choice: str) -> None:
    """Rock paper scissors. Usage: !rps rock"""
    c = choice.lower().rstrip("s")
    if c not in ("rock", "paper", "scissor"):
        c = {"r": "rock", "p": "paper", "s": "scissor"}.get(c, "")
    if not c:
        await ctx.send(embed=err("Choose `rock`, `paper` or `scissors`."))
        return
    bot_choice = random.choice(["rock", "paper", "scissor"])
    if bot_choice == c:
        verdict = "It's a tie! 🤝"
    elif (c, bot_choice) in {("rock", "scissor"), ("paper", "rock"), ("scissor", "paper")}:
        verdict = "You win! 🎉"
    else:
        verdict = "I win! 🤖"
    await ctx.send(embed=fun(f"You: **{c}**\nMe: **{bot_choice}**\n\n{verdict}", "✊✋✌️"))


JOKES = [
    "Why don't skeletons fight each other? They don't have the guts.",
    "I told my Wi-Fi we needed to talk. Now we have no connection.",
    "Why did the scarecrow win an award? He was outstanding in his field.",
    "I'm reading a book about anti-gravity. It's impossible to put down.",
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "What do you call a fake noodle? An impasta.",
    "I used to hate facial hair, but then it grew on me.",
    "Why did the coffee file a police report? It got mugged.",
    "What's a skeleton's least favourite instrument? The trom-bone.",
    "I only know 25 letters of the alphabet. I don't know y.",
]

DADJOKES = [
    "I'm afraid for the calendar. Its days are numbered.",
    "What do you call a factory that makes okay products? A satisfactory.",
    "Did you hear about the cheese factory exploding? There was nothing left but de-brie.",
    "I only have one child... named Kyle.",
    "Why can't your nose be 12 inches long? Because then it'd be a foot.",
    "I don't trust stairs. They're always up to something.",
]

QUOTES = [
    "“The secret of getting ahead is getting started.” — Mark Twain",
    "“It always seems impossible until it's done.” — Nelson Mandela",
    "“Whether you think you can or you can't, you're right.” — Henry Ford",
    "“Do or do not. There is no try.” — Yoda",
    "“Fall seven times, stand up eight.” — Japanese proverb",
    "“Simplicity is the soul of efficiency.” — Austin Freeman",
    "“The best way to predict the future is to invent it.” — Alan Kay",
]

FACTS = [
    "Honey never spoils — 3,000-year-old honey from Egyptian tombs is still edible.",
    "Octopuses have three hearts and blue blood.",
    "A day on Venus is longer than a year on Venus.",
    "Bananas are berries, but strawberries aren't.",
    "Sharks existed before trees did.",
    "The Eiffel Tower grows about 15 cm taller in summer from thermal expansion.",
    "Wombats produce cube-shaped droppings.",
    "There are more possible chess games than atoms in the observable universe.",
    "Sea otters hold hands while sleeping so they don't drift apart.",
    "A cloud can weigh over a million pounds.",
]

COMPLIMENTS = [
    "You're the human equivalent of a perfectly timed coffee.",
    "Your energy is genuinely contagious — in the good way.",
    "You make this server noticeably better just by being in it.",
    "If good vibes were a currency, you'd be rich beyond measure.",
    "You have excellent taste in Discord servers, clearly.",
    "Your brain is doing great work today.",
    "You're the reason people stay in this server.",
    "Nobody does it quite like you — and that's a compliment.",
]

ROASTS = [
    "You're the reason shampoo has instructions.",
    "You bring everyone so much joy... when you leave the room.",
    "Your keyboard must hate you for how you type.",
    "You're not the dumbest person in the room, but better hope they don't leave.",
    "You have the confidence of someone who's never been checked.",
    "If laziness were an Olympic sport, you'd finish fourth and be proud of it.",
    "You're proof that training doesn't always stick.",
    "Your opinions are like your Wi-Fi — mostly noise, sometimes a signal.",
]

RIDDLES = [
    ("I speak without a mouth and hear without ears. I have no body, but I come alive with wind. What am I?", "an echo"),
    ("The more you take, the more you leave behind. What am I?", "footsteps"),
    ("I have keys but no locks, space but no room. What am I?", "a keyboard"),
    ("What has a head, a tail, but no body?", "a coin"),
    ("What gets wetter the more it dries?", "a towel"),
    ("I'm tall when I'm young and short when I'm old. What am I?", "a candle"),
    ("What can travel around the world while staying in a corner?", "a stamp"),
]

FORTUNES = [
    "A golden opportunity is closer than it looks.",
    "Say yes to the thing you keep almost doing.",
    "Your next upload is going to do numbers.",
    "Someone you haven't spoken to in a while will surprise you.",
    "Good things come to those who post consistently.",
    "Expect a small win in the next 48 hours.",
    "The answer you've been circling is the obvious one.",
    "Sleep earlier tonight. It'll fix more than you think.",
]


@bot.hybrid_command(name="joke")
async def joke(ctx: commands.Context) -> None:
    """Tell a random joke."""
    await ctx.send(embed=fun(random.choice(JOKES), "😂"))


@bot.hybrid_command(name="dadjoke")
async def dadjoke(ctx: commands.Context) -> None:
    """A painfully dad-tier joke."""
    await ctx.send(embed=fun(random.choice(DADJOKES), "👨 Joke"))


@bot.hybrid_command(name="quote")
async def quote(ctx: commands.Context) -> None:
    """Random motivational quote."""
    await ctx.send(embed=fun(random.choice(QUOTES), "💬 Quote"))


@bot.hybrid_command(name="fact")
async def fact(ctx: commands.Context) -> None:
    """Random useless fact."""
    await ctx.send(embed=fun(random.choice(FACTS), "🧠 Fact"))


@bot.hybrid_command(name="compliment")
async def compliment(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    """Compliment someone."""
    target = member or ctx.author
    await ctx.send(embed=fun(f"{target.mention} {random.choice(COMPLIMENTS)}", "💐"))


@bot.hybrid_command(name="roast")
async def roast(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    """Playfully roast someone."""
    target = member or ctx.author
    await ctx.send(embed=fun(f"{target.mention} {random.choice(ROASTS)}", "🔥"))


@bot.hybrid_command(name="ship")
async def ship(ctx: commands.Context, first: str, second: Optional[str] = None) -> None:
    """Ship two users. Usage: !ship @a @b"""
    a = first
    b = second or str(ctx.author)
    percent = seed("ship", a, b) % 101
    bar = "█" * (percent // 10) + "░" * (10 - percent // 10)
    verdict = "Perfect match 💞" if percent > 85 else "Could work 👀" if percent > 60 else \
              "Mild vibes 😐" if percent > 35 else "Run 🏃"
    await ctx.send(embed=fun(f"{a} ❤️ {b}\n`{bar}` **{percent}%**\n{verdict}", "💘 Ship"))


@bot.hybrid_command(name="rate")
async def rate(ctx: commands.Context, *, thing: str) -> None:
    """Rate anything out of 10."""
    score = seed("rate", thing) % 11
    await ctx.send(embed=fun(f"**{thing}** → **{score}/10**\n" + "⭐" * score, "📊 Rating"))


@bot.hybrid_command(name="iq")
async def iq(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    """Totally scientific IQ test."""
    target = member or ctx.author
    await ctx.send(embed=fun(f"🧠 **{target.display_name}**'s IQ: **{60 + seed('iq', target.id) % 85}**", "IQ"))


@bot.hybrid_command(name="gayrate", aliases=["gay"])
async def gayrate(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    """Silly 0-100 metric. All in good fun."""
    target = member or ctx.author
    await ctx.send(embed=fun(f"🌈 **{target.display_name}** is **{seed('g', target.id) % 101}%** today.", "🌈"))


@bot.hybrid_command(name="reverse")
async def reverse(ctx: commands.Context, *, text: str) -> None:
    """Reverse your text."""
    await ctx.send(embed=fun(f"`{text[::-1][:1900]}`"))


@bot.hybrid_command(name="mock")
async def mock(ctx: commands.Context, *, text: str) -> None:
    """sPoNgEbOb-ify your text."""
    out = "".join(c.upper() if i % 2 else c.lower() for i, c in enumerate(text))
    await ctx.send(embed=fun(f"`{out[:1900]}`"))


@bot.hybrid_command(name="emojify")
async def emojify(ctx: commands.Context, *, text: str) -> None:
    """Turn text into regional-indicator emoji letters."""
    out = []
    for ch in text.lower()[:120]:
        if "a" <= ch <= "z":
            out.append(chr(0x1F1E6 + ord(ch) - ord("a")))
        elif ch == " ":
            out.append("  ")
        else:
            out.append(ch)
    await ctx.send(" ".join(out))


@bot.hybrid_command(name="clap")
async def clap(ctx: commands.Context, *, text: str) -> None:
    """Add 👏 between words."""
    await ctx.send(" 👏 ".join(text.split())[:1900])


@bot.hybrid_command(name="scream")
async def scream(ctx: commands.Context, *, text: str) -> None:
    """YELL YOUR TEXT."""
    await ctx.send(embed=fun(f"**{text.upper()}!!!**"))


@bot.hybrid_command(name="spoiler")
async def spoiler(ctx: commands.Context, *, text: str) -> None:
    """Hide text behind a spoiler tag."""
    await ctx.send(f"||{text[:1900]}||")


@bot.hybrid_command(name="hug")
async def hug(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    """Hug someone."""
    target = member or ctx.author
    await ctx.send(embed=fun(f"🤗 **{ctx.author.display_name}** hugs **{target.display_name}**!"))


@bot.hybrid_command(name="pat")
async def pat(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    """Pat someone on the head."""
    target = member or ctx.author
    await ctx.send(embed=fun(f"👋 **{ctx.author.display_name}** pats **{target.display_name}**."))


@bot.hybrid_command(name="slap")
async def slap(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    """Slap someone with a large fish."""
    target = member or ctx.author
    await ctx.send(embed=fun(f"🐟 **{ctx.author.display_name}** slaps **{target.display_name}** with a trout."))


@bot.hybrid_command(name="highfive")
async def highfive(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    """High five someone."""
    target = member or ctx.author
    await ctx.send(embed=fun(f"🙌 **{ctx.author.display_name}** high-fives **{target.display_name}**!"))


@bot.hybrid_command(name="slots")
async def slots(ctx: commands.Context) -> None:
    """Spin the slot machine."""
    symbols = ["🍒", "🍋", "🔔", "⭐", "💎", "7️⃣"]
    result = [random.choice(symbols) for _ in range(3)]
    if len(set(result)) == 1:
        verdict = "🎉 JACKPOT!"
    elif len(set(result)) == 2:
        verdict = "✨ Two of a kind!"
    else:
        verdict = "💸 No luck."
    await ctx.send(embed=fun(f"{' | '.join(result)}\n\n{verdict}", "🎰 Slots"))


@bot.hybrid_command(name="trivia")
async def trivia(ctx: commands.Context) -> None:
    """Answer a trivia question within 15 seconds."""
    questions = [
        ("What planet is known as the Red Planet?", "mars"),
        ("How many continents are there?", "7"),
        ("What is the largest ocean on Earth?", "pacific"),
        ("Who painted the Mona Lisa?", "da vinci"),
        ("What is the chemical symbol for gold?", "au"),
        ("How many sides does a hexagon have?", "6"),
        ("What year did the first YouTube video go up?", "2005"),
        ("What is the capital of Japan?", "tokyo"),
    ]
    question, answer = random.choice(questions)
    await ctx.send(embed=info(f"**{question}**\nYou have 15 seconds.", "❓ Trivia"))

    def check(m: discord.Message) -> bool:
        return m.channel == ctx.channel and not m.author.bot

    try:
        msg = await bot.wait_for("message", timeout=15.0, check=check)
    except asyncio.TimeoutError:
        await ctx.send(embed=err(f"⏰ Time's up! The answer was **{answer}**."))
        return
    if answer in msg.content.lower():
        await msg.reply(embed=ok("✅ Correct!", "🎉"))
    else:
        await msg.reply(embed=err(f"❌ Not quite. The answer was **{answer}**."))


@bot.hybrid_command(name="riddle")
async def riddle(ctx: commands.Context) -> None:
    """Give a riddle with the answer hidden in a spoiler."""
    q, a = random.choice(RIDDLES)
    await ctx.send(embed=fun(f"{q}\n\nAnswer: ||{a}||", "🧩 Riddle"))


@bot.hybrid_command(name="fortune")
async def fortune(ctx: commands.Context) -> None:
    """Open a fortune cookie."""
    await ctx.send(embed=fun(f"🥠 {random.choice(FORTUNES)}", "Fortune"))


@bot.hybrid_command(name="numberbetween", aliases=["guess"])
async def numberbetween(ctx: commands.Context, low: int = 1, high: int = 10) -> None:
    """Guess a number between two values in 10 seconds."""
    if low > high:
        low, high = high, low
    if high - low > 1000:
        await ctx.send(embed=err("Keep the range under 1000."))
        return
    secret = random.randint(low, high)
    await ctx.send(embed=info(f"I picked a number between **{low}** and **{high}**. Guess it (10s)!", "🔢"))

    def check(m: discord.Message) -> bool:
        return m.channel == ctx.channel and not m.author.bot

    try:
        msg = await bot.wait_for("message", timeout=10.0, check=check)
    except asyncio.TimeoutError:
        await ctx.send(embed=err(f"⏰ Time's up — it was **{secret}**."))
        return
    try:
        guess = int(msg.content.strip())
    except ValueError:
        await ctx.send(embed=err(f"Not a number. It was **{secret}**."))
        return
    if guess == secret:
        await msg.reply(embed=ok(f"🎯 Correct! It was **{secret}**."))
    else:
        await msg.reply(embed=err(f"Nope — it was **{secret}**."))


@bot.hybrid_command(name="bofh", aliases=["excuse"])
async def bofh(ctx: commands.Context) -> None:
    """Random tech excuse."""
    excuses = [
        "Cosmic rays flipped a bit. Nothing we can do.",
        "It works on my machine, so it's a you problem.",
        "That's a known issue with the version you're not on.",
        "The server was rebooted by the cleaning staff.",
        "DNS. It's always DNS.",
        "Someone pushed directly to main.",
        "The cache is angry. Give it 20 minutes.",
        "It's not a bug, it's an undocumented feature.",
    ]
    await ctx.send(embed=fun(random.choice(excuses), "🧑‍💻 Excuse"))


@bot.hybrid_command(name="owo")
async def owo(ctx: commands.Context, *, text: str = "hello") -> None:
    """owo-ify text."""
    table = str.maketrans({"l": "w", "r": "w", "L": "W", "R": "W"})
    await ctx.send(embed=fun(f"{text.translate(table)} owo"))


@bot.hybrid_command(name="tableflip")
async def tableflip(ctx: commands.Context) -> None:
    """(╯°□°）╯︵ ┻━┻"""
    await ctx.send("(╯°□°）╯︵ ┻━┻")


@bot.hybrid_command(name="unflip")
async def unflip(ctx: commands.Context) -> None:
    """┬─┬ ノ( ゜-゜ノ)"""
    await ctx.send("┬─┬ ノ( ゜-゜ノ)")


@bot.hybrid_command(name="lenny")
async def lenny(ctx: commands.Context) -> None:
    """( ͡° ͜ʖ ͡°)"""
    await ctx.send("( ͡° ͜ʖ ͡°)")


@bot.hybrid_command(name="vibecheck")
async def vibecheck(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    """Check someone's vibes."""
    target = member or ctx.author
    score = seed("vibe", target.id) % 101
    verdict = "immaculate ✨" if score > 80 else "solid 😎" if score > 50 else "concerning 😐" if score > 25 else "failed 💀"
    await ctx.send(embed=fun(f"**{target.display_name}**'s vibe: **{score}/100** — {verdict}", "🎧 Vibe check"))


# ===========================================================================
# 5. HANDLE WATCHER  ->  DM the owner when @themasteroogway_ is mentioned
# ===========================================================================

def _core(handle: str) -> str:
    return re.sub(r"[^a-z0-9]", "", handle.lower())


def _compile(handle: str) -> Optional[re.Pattern[str]]:
    letters = _core(handle)
    if not letters:
        return None
    body = r"[\W_]{0,3}".join(re.escape(c) for c in letters)
    return re.compile(rf"@?{body}", re.IGNORECASE)


WATCH_PATTERN = _compile(WATCH_HANDLE)
_last_notify: dict[int, float] = {}


def find_mentions(text: str) -> list[str]:
    if WATCH_PATTERN is None:
        return []
    return [m.group(0) for m in WATCH_PATTERN.finditer(text)]


@bot.hybrid_command(name="notify")
async def notify(ctx: commands.Context, *, text: Optional[str] = None) -> None:
    """DM the owner when your message contains @themasteroogway_."""
    if not text:
        await ctx.send(embed=info(
            f"**Usage:** `{PREFIX}notify <your message>`\n"
            f"**Example:** `{PREFIX}notify new upload from {WATCH_HANDLE}`\n"
            f"A DM is only sent if the message contains the handle."
        ))
        return

    hits = find_mentions(text)
    if not hits:
        await ctx.send(embed=err(f"Nothing sent — that didn't contain `{WATCH_HANDLE}`."))
        return

    now = time.monotonic()
    last = _last_notify.get(ctx.author.id, 0.0)
    if now - last < NOTIFY_COOLDOWN:
        await ctx.send(embed=err(f"Slow down — try again in {int(NOTIFY_COOLDOWN - (now - last))}s."))
        return
    _last_notify[ctx.author.id] = now

    owner = await get_owner()
    if owner is None:
        await ctx.send(embed=err("Owner isn't configured (set `OWNER_ID`)."))
        return

    embed = discord.Embed(
        title=f"New {WATCH_HANDLE} mention",
        description=(
            f"**User:** {ctx.author} (`{ctx.author.id}`)\n"
            f"**Text:** {text[:1500]}\n"
            f"**Matched:** {', '.join(f'`{h}`' for h in hits)}"
        ),
        colour=COLOUR_FUN,
    )
    try:
        await owner.send(embed=embed)
    except discord.Forbidden:
        await ctx.send(embed=err("I can't DM the owner — their DMs are closed."))
        if ctx.guild:
            await log_action(ctx.guild, f"Unsent notify from {ctx.author}: {text}")
        return
    try:
        if ctx.message is not None:
            await ctx.message.add_reaction("📩")
    except Exception:
        pass
    await ctx.send(embed=ok("Sent to the owner's DMs 📩"))


@bot.hybrid_command(name="watched")
async def watched(ctx: commands.Context) -> None:
    """Show which handle triggers a DM."""
    await ctx.send(embed=info(f"Watching for: `{WATCH_HANDLE}`\nAny spelling works — "
                              f"`@The Master Oogway`, `themasteroogway_`, full channel URLs."))


# ===========================================================================
# 6. GIVEAWAYS
# ===========================================================================

@dataclass
class Giveaway:
    message_id: int
    channel_id: int
    prize: str
    winner_count: int
    ends_at: float
    host_id: int
    entrants: set[int] = field(default_factory=set)
    ended: bool = False
    winners: list[int] = field(default_factory=list)


GIVEAWAYS: dict[int, Giveaway] = {}


def gw_embed(gw: Giveaway) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎉 {gw.prize}",
        description=(
            "Press **Enter** below for a chance to win!\n"
            "Press it again to leave.\n\n"
            f"**Winners:** {gw.winner_count}\n"
            f"**Entries:** {len(gw.entrants)}\n"
            f"**Ends:** <t:{int(gw.ends_at)}:R> (<t:{int(gw.ends_at)}:f>)"
        ),
        colour=COLOUR_FUN,
    )
    embed.set_footer(text=f"Hosted by {gw.host_id} • Giveaway ID {gw.message_id}")
    return embed


def gw_ended_embed(gw: Giveaway) -> discord.Embed:
    if gw.winners:
        desc = "**Winners:** " + ", ".join(f"<@{w}>" for w in gw.winners)
    else:
        desc = "**Winners:** none — nobody entered. 😢"
    embed = discord.Embed(
        title=f"🎉 {gw.prize} — ENDED",
        description=f"{desc}\n\n**Entries:** {len(gw.entrants)}",
        colour=COLOUR_OK if gw.winners else COLOUR_ERR,
    )
    embed.set_footer(text=f"Giveaway ID {gw.message_id}")
    return embed


async def end_giveaway(gw: Giveaway) -> None:
    """Close a giveaway, edit its message and announce the winners."""
    if gw.ended:
        return
    gw.ended = True

    channel = bot.get_channel(gw.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(gw.channel_id)
        except Exception:
            return

    entrants = [uid for uid in gw.entrants]
    if entrants and gw.winner_count > 0:
        gw.winners = random.sample(entrants, min(gw.winner_count, len(entrants)))

    try:
        message = await channel.fetch_message(gw.message_id)  # type: ignore[union-attr]
        await message.edit(embed=gw_ended_embed(gw), view=None)
    except Exception:
        pass

    if gw.winners:
        try:
            await channel.send(  # type: ignore[union-attr]
                content=" ".join(f"<@{w}>" for w in gw.winners),
                embed=ok(f"You won **{gw.prize}**! Contact the host to claim it.", "🎊 Winner"),
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except Exception:
            pass

    save_data()  # keep the finished result for !greroll after a restart


async def _gw_timer(gw: Giveaway) -> None:
    delay = max(0.0, gw.ends_at - time.time())
    try:
        await asyncio.sleep(delay)
        await end_giveaway(gw)
    except asyncio.CancelledError:
        pass


class GiveawayView(discord.ui.View):
    """Persistent enter button (survives restarts)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Enter", emoji="🎉", style=discord.ButtonStyle.success, custom_id="gw:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        gw = GIVEAWAYS.get(interaction.message.id) if interaction.message else None
        if gw is None:
            await interaction.response.send_message("This giveaway is no longer tracked.", ephemeral=True)
            return
        if gw.ended:
            await interaction.response.send_message("This giveaway has already ended.", ephemeral=True)
            return

        if interaction.user.id in gw.entrants:
            gw.entrants.discard(interaction.user.id)
            await interaction.response.send_message("You left the giveaway.", ephemeral=True)
        else:
            gw.entrants.add(interaction.user.id)
            await interaction.response.send_message(
                f"You're in! Total entries: **{len(gw.entrants)}**", ephemeral=True
            )
        mark_dirty()  # flushed within a few seconds
        try:
            await interaction.message.edit(embed=gw_embed(gw))
        except Exception:
            pass


@bot.hybrid_command(name="gstart", aliases=["giveaway"], usage="<time> <winners> <prize>")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def gstart(ctx: commands.Context, duration: str, winners: int, *, prize: str) -> None:
    """Start a giveaway. Usage: !gstart 10m 1 Nitro Classic"""
    seconds = parse_duration(duration)
    if seconds < 5:
        await ctx.send(embed=err("Duration must be at least 5 seconds — e.g. `30s`, `10m`, `2h`, `1d`."))
        return
    if seconds > 30 * 86400:
        await ctx.send(embed=err("Keep giveaways under 30 days."))
        return

    winners = max(1, min(winners, 50))
    gw = Giveaway(
        message_id=0,
        channel_id=ctx.channel.id,
        prize=prize[:200],
        winner_count=winners,
        ends_at=time.time() + seconds,
        host_id=ctx.author.id,
    )
    msg = await ctx.send(embed=gw_embed(gw), view=GiveawayView())
    gw.message_id = msg.id
    GIVEAWAYS[msg.id] = gw
    save_data()
    asyncio.create_task(_gw_timer(gw))

    await ctx.send(
        embed=ok(
            f"Giveaway for **{prize}** started in {ctx.channel.mention}.\n"
            f"Ends in **{fmt_duration(seconds)}** • ID `{msg.id}`",
            "🎉 Giveaway started",
        )
    )


@bot.hybrid_command(name="gend", aliases=["giveawayend"])
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def gend(ctx: commands.Context, message_id: Optional[int] = None) -> None:
    """End a giveaway early. Usage: !gend 1234567890 (or !gend for the newest one)"""
    gw = _find_giveaway(ctx, message_id)
    if gw is None:
        await ctx.send(embed=err("No active giveaway found here. Pass the giveaway message ID."))
        return
    if gw.ended:
        await ctx.send(embed=err("That giveaway has already ended."))
        return
    await end_giveaway(gw)
    await ctx.send(embed=ok(f"Ended **{gw.prize}** — {len(gw.winners)} winner(s) picked."))


@bot.hybrid_command(name="greroll", aliases=["giveawayreroll"])
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def greroll(ctx: commands.Context, message_id: Optional[int] = None) -> None:
    """Pick new winners for a finished giveaway. Usage: !greroll 1234567890"""
    gw = _find_giveaway(ctx, message_id)
    if gw is None:
        await ctx.send(embed=err("Giveaway not found. Pass the message ID."))
        return
    if not gw.ended:
        await ctx.send(embed=err("That giveaway is still running — use `!gend` first."))
        return

    pool = [uid for uid in gw.entrants if uid not in gw.winners]
    if not pool:
        pool = list(gw.entrants)
    if not pool:
        await ctx.send(embed=err("Nobody entered that giveaway."))
        return

    picks = random.sample(pool, min(gw.winner_count, len(pool)))
    gw.winners = picks
    save_data()
    channel = bot.get_channel(gw.channel_id) or ctx.channel
    await channel.send(  # type: ignore[union-attr]
        content=" ".join(f"<@{w}>" for w in picks),
        embed=ok(f"Rerolled! New winner(s) for **{gw.prize}**.", "🔄 Reroll"),
        allowed_mentions=discord.AllowedMentions(users=True),
    )


def _find_giveaway(ctx: commands.Context, message_id: Optional[int]) -> Optional[Giveaway]:
    if message_id:
        return GIVEAWAYS.get(message_id)
    for gw in GIVEAWAYS.values():
        if gw.channel_id == ctx.channel.id and not gw.ended:
            return gw
    for gw in GIVEAWAYS.values():
        if gw.channel_id == ctx.channel.id:
            return gw
    return None


@bot.hybrid_command(name="gentries", aliases=["glist"])
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def gentries(ctx: commands.Context, message_id: Optional[int] = None) -> None:
    """List who entered a giveaway (staff only)."""
    gw = _find_giveaway(ctx, message_id)
    if gw is None:
        await ctx.send(embed=err("Giveaway not found."))
        return
    if not gw.entrants:
        await ctx.send(embed=info(f"**{gw.prize}** has no entries yet."))
        return
    names = ", ".join(f"<@{uid}>" for uid in list(gw.entrants)[:40])
    await ctx.send(embed=info(f"Entries ({len(gw.entrants)}):\n{names}", f"🎉 {gw.prize}"))


# ===========================================================================
# 7. TICKETS
# ===========================================================================

TICKET_MARKER = "ticket-owner:"
TRANSCRIPT_DIR: str = os.getenv("TRANSCRIPT_DIR", "transcripts")
MAX_TICKET_HISTORY = 100


@dataclass
class Ticket:
    channel_id: int
    guild_id: int
    owner_id: int
    opened_at: float
    claimed_by: int = 0
    status: str = "open"          # "open" or "closed"
    closed_at: float = 0.0
    closed_by: int = 0


TICKETS: dict[int, Ticket] = {}    # keyed by channel id


def ticket_owner_id(channel: discord.abc.GuildChannel) -> Optional[int]:
    """Read the opener's ID from the channel topic (survives restarts on its own)."""
    topic = getattr(channel, "topic", None) or ""
    match = re.search(rf"{TICKET_MARKER}(\d+)", topic)
    return int(match.group(1)) if match else None


def open_ticket_count(guild_id: Optional[int] = None) -> int:
    return sum(
        1 for t in TICKETS.values()
        if t.status == "open" and (guild_id is None or t.guild_id == guild_id)
    )


async def save_transcript(channel: discord.TextChannel, owner_id: Optional[int]) -> Optional[str]:
    """Write the conversation to a .txt file before the channel is deleted."""
    try:
        os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
        lines = [
            f"Transcript for #{channel.name}",
            f"Server: {channel.guild.name} ({channel.guild.id})",
            f"Channel ID: {channel.id}",
            f"Opened by: {owner_id}",
            f"Closed: {discord.utils.utcnow():%Y-%m-%d %H:%M:%S} UTC",
            "-" * 60,
        ]
        count = 0
        async for msg in channel.history(limit=1000, oldest_first=True):
            stamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            body = msg.content or ""
            if msg.attachments:
                body += " " + " ".join(a.url for a in msg.attachments)
            if msg.embeds and not body:
                body = "[embed]"
            lines.append(f"[{stamp}] {msg.author}: {body}")
            count += 1
        lines.append("-" * 60)
        lines.append(f"{count} message(s)")

        path = os.path.join(
            TRANSCRIPT_DIR, f"{channel.name}-{int(time.time())}.txt"
        )
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        print(f"Transcript saved: {path}")
        return path
    except Exception as exc:
        print(f"Could not write transcript: {exc}")
        return None


async def close_ticket(
    channel: discord.TextChannel,
    closed_by: discord.abc.User,
    announce: bool = True,
) -> str:
    """Close a ticket: transcript, mark the record closed, tell the log, delete the channel."""
    ticket = TICKETS.get(channel.id)
    owner_id = ticket.owner_id if ticket else ticket_owner_id(channel)

    path = await save_transcript(channel, owner_id)

    if ticket is None:
        ticket = Ticket(
            channel_id=channel.id,
            guild_id=channel.guild.id,
            owner_id=owner_id or 0,
            opened_at=channel.created_at.timestamp(),
        )
        TICKETS[channel.id] = ticket
    ticket.status = "closed"
    ticket.closed_at = time.time()
    ticket.closed_by = closed_by.id
    save_data()

    # Drop the transcript into the log channel if one is configured
    if LOG_CHANNEL_ID and path:
        log_channel = channel.guild.get_channel(LOG_CHANNEL_ID)
        if log_channel is not None:
            try:
                await log_channel.send(
                    embed=info(
                        f"**Ticket:** #{channel.name}\n"
                        f"**Opened by:** <@{owner_id}>\n"
                        f"**Closed by:** {closed_by.mention}",
                        "📄 Ticket closed",
                    ),
                    file=discord.File(path),
                )
            except Exception as exc:
                print(f"Could not send transcript to log channel: {exc}")

    await log_action(channel.guild, f"Ticket #{channel.name} closed by {closed_by}")

    if announce and isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=ok("🔒 Deleting this channel in 5 seconds..."))
        except Exception:
            pass
        await asyncio.sleep(5)

    try:
        await channel.delete(reason=f"Ticket closed by {closed_by}")
    except Exception:
        pass
    return path or ""


async def reconcile_tickets() -> None:
    """Re-sync the ticket registry with what actually exists in Discord.

    Runs on every login, so tickets survive restarts even if botdata.json is
    lost or was never written.
    """
    live: set[int] = set()
    added = 0
    for guild in bot.guilds:
        for channel in guild.text_channels:
            owner = ticket_owner_id(channel)
            if owner is None:
                continue
            live.add(channel.id)
            if channel.id not in TICKETS:
                TICKETS[channel.id] = Ticket(
                    channel_id=channel.id,
                    guild_id=guild.id,
                    owner_id=owner,
                    opened_at=channel.created_at.timestamp(),
                )
                added += 1

    # Any ticket whose channel disappeared is considered closed
    orphans = 0
    for ticket in TICKETS.values():
        if ticket.status == "open" and ticket.channel_id not in live:
            ticket.status = "closed"
            ticket.closed_at = ticket.closed_at or time.time()
            orphans += 1

    # Keep only the most recent closed records
    closed = sorted((t for t in TICKETS.values() if t.status == "closed"),
                    key=lambda t: t.closed_at, reverse=True)
    for old in closed[MAX_TICKET_HISTORY:]:
        TICKETS.pop(old.channel_id, None)

    if added or orphans:
        print(f"Ticket reconciliation: +{added} recovered, {orphans} marked closed.")
    print(f"Open tickets: {open_ticket_count()}")
    save_data()


class TicketCloseView(discord.ui.View):
    """Persistent close button shown inside a ticket channel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Close ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Not a ticket channel.", ephemeral=True)
            return

        owner_id = ticket_owner_id(channel)
        is_owner = owner_id is not None and interaction.user.id == owner_id
        is_staff = bool(getattr(interaction.user.guild_permissions, "manage_channels", False))
        if not (is_owner or is_staff):
            await interaction.response.send_message("Only the ticket opener or staff can close this.", ephemeral=True)
            return

        await interaction.response.send_message("🔒 Closed — saving transcript and deleting in 5 seconds...")
        await close_ticket(channel, interaction.user, announce=False)


class TicketPanelView(discord.ui.View):
    """Persistent 'Open a ticket' button."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Open a ticket", emoji="📩", style=discord.ButtonStyle.primary, custom_id="ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Tickets only work in a server.", ephemeral=True)
            return

        # One open ticket per person
        for ch in guild.text_channels:
            if ticket_owner_id(ch) == interaction.user.id:
                await interaction.response.send_message(
                    f"You already have a ticket open: {ch.mention}", ephemeral=True
                )
                return

        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True,
            ),
        }
        if guild.me is not None:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_channels=True, attach_files=True, embed_links=True,
            )
        support_role = guild.get_role(SUPPORT_ROLE_ID) if SUPPORT_ROLE_ID else None
        if support_role is not None:
            overwrites[support_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True,
            )

        category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
        name = re.sub(r"[^a-z0-9-]", "", interaction.user.name.lower().replace(" ", "-"))[:20] or "user"

        try:
            channel = await guild.create_text_channel(
                f"ticket-{name}",
                overwrites=overwrites,
                category=category,  # type: ignore[arg-type]
                topic=f"{TICKET_MARKER}{interaction.user.id}",
                reason=f"Ticket opened by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I need **Manage Channels** to create tickets.", ephemeral=True
            )
            return

        # Persist the ticket record immediately, so a crash can't lose it
        TICKETS[channel.id] = Ticket(
            channel_id=channel.id,
            guild_id=guild.id,
            owner_id=interaction.user.id,
            opened_at=time.time(),
        )
        save_data()

        ping = interaction.user.mention + (f" <@&{SUPPORT_ROLE_ID}>" if support_role else "")
        await channel.send(
            content=ping,
            embed=info(
                f"Hey {interaction.user.mention}, thanks for reaching out!\n"
                "Describe your issue and a staff member will be with you shortly.\n\n"
                "Press **Close ticket** when you're done.",
                "📩 Support ticket",
            ),
            view=TicketCloseView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=bool(support_role)),
        )
        await interaction.response.send_message(f"Your ticket is open: {channel.mention}", ephemeral=True)
        await log_action(guild, f"{interaction.user} opened ticket {channel.mention} ({open_ticket_count(guild.id)} open now)")


@bot.hybrid_command(name="ticketpanel", aliases=["tpanel", "ticket"])
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
@commands.bot_has_permissions(manage_channels=True)
async def ticketpanel(ctx: commands.Context) -> None:
    """Post the 'Open a ticket' panel in this channel."""
    embed = discord.Embed(
        title="📩 Support tickets",
        description=(
            "Need help? Press the button below and a **private channel** will be "
            "created just for you and the staff team.\n\n"
            "• One ticket per person at a time\n"
            "• Only you and staff can see it\n"
            "• Press **Close ticket** inside to finish"
        ),
        colour=COLOUR_INFO,
    )
    await ctx.send(embed=embed, view=TicketPanelView())
    await ctx.send(embed=ok("Ticket panel posted."), delete_after=5)


@bot.hybrid_command(name="tclose", aliases=["closeticket"])
@commands.guild_only()
async def tclose(ctx: commands.Context) -> None:
    """Close the current ticket channel (saves a transcript first)."""
    owner_id = ticket_owner_id(ctx.channel)
    is_owner = owner_id is not None and ctx.author.id == owner_id
    is_staff = bool(getattr(ctx.author.guild_permissions, "manage_channels", False))
    if not (is_owner or is_staff):
        await ctx.send(embed=err("Only the ticket opener or staff can close this ticket."))
        return
    await close_ticket(ctx.channel, ctx.author)


@bot.hybrid_command(name="tlist", aliases=["tickets"])
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
async def tlist(ctx: commands.Context) -> None:
    """List every open ticket on this server (staff only)."""
    open_tickets = [t for t in TICKETS.values() if t.status == "open" and t.guild_id == ctx.guild.id]
    if not open_tickets:
        await ctx.send(embed=info("No open tickets right now.", "🎫 Tickets"))
        return

    lines = []
    for t in sorted(open_tickets, key=lambda t: t.opened_at)[:25]:
        channel = ctx.guild.get_channel(t.channel_id)
        where = channel.mention if channel else f"`deleted` ({t.channel_id})"
        claimed = f"<@{t.claimed_by}>" if t.claimed_by else "unclaimed"
        age = fmt_duration(time.time() - t.opened_at)
        lines.append(f"{where} — <@{t.owner_id}> • {age} • {claimed}")

    embed = discord.Embed(
        title=f"🎫 Open tickets ({len(open_tickets)})",
        description="\n".join(lines)[:4000],
        colour=COLOUR_INFO,
    )
    closed = sum(1 for t in TICKETS.values() if t.status == "closed")
    embed.set_footer(text=f"{closed} closed ticket(s) in history • survives restarts")
    await ctx.send(embed=embed)


@bot.hybrid_command(name="tclaim")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
async def tclaim(ctx: commands.Context) -> None:
    """Claim the current ticket so others know you're handling it."""
    if ticket_owner_id(ctx.channel) is None:
        await ctx.send(embed=err("This isn't a ticket channel."))
        return
    ticket = TICKETS.get(ctx.channel.id)
    if ticket is None:
        ticket = Ticket(
            channel_id=ctx.channel.id, guild_id=ctx.guild.id,
            owner_id=ticket_owner_id(ctx.channel) or 0,
            opened_at=ctx.channel.created_at.timestamp(),
        )
        TICKETS[ctx.channel.id] = ticket
    if ticket.claimed_by == ctx.author.id:
        await ctx.send(embed=err("You already claimed this ticket."))
        return
    ticket.claimed_by = ctx.author.id
    save_data()
    await ctx.send(embed=ok(f"{ctx.author.mention} is handling this ticket.", "🎯 Claimed"))


@bot.hybrid_command(name="tadd")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
async def tadd(ctx: commands.Context, member: discord.Member) -> None:
    """Add a user to the current ticket."""
    if ticket_owner_id(ctx.channel) is None:
        await ctx.send(embed=err("This isn't a ticket channel."))
        return
    await ctx.channel.set_permissions(
        member, view_channel=True, send_messages=True, read_message_history=True, attach_files=True
    )
    await ctx.send(embed=ok(f"Added {member.mention} to this ticket."))


@bot.hybrid_command(name="tremove")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
async def tremove(ctx: commands.Context, member: discord.Member) -> None:
    """Remove a user from the current ticket."""
    if ticket_owner_id(ctx.channel) is None:
        await ctx.send(embed=err("This isn't a ticket channel."))
        return
    await ctx.channel.set_permissions(member, overwrite=None)
    await ctx.send(embed=ok(f"Removed {member.mention} from this ticket."))


# ===========================================================================
# 8. PERSISTENCE  (warnings + giveaways survive restarts)
# ===========================================================================

MAX_ENDED_GIVEAWAYS = 50
MAX_WARNINGS_PER_USER = 50

_dirty = False
_last_save = 0.0
_restored = False
_save_errors = 0


def mark_dirty() -> None:
    """Queue a save; the background flusher writes it within a few seconds."""
    global _dirty
    _dirty = True


def _serialize() -> dict:
    active = [g for g in GIVEAWAYS.values() if not g.ended]
    ended = sorted((g for g in GIVEAWAYS.values() if g.ended),
                   key=lambda g: g.ends_at, reverse=True)[:MAX_ENDED_GIVEAWAYS]
    keep = active + ended

    # Drop the oldest finished giveaways from memory as well
    for gid in [g.message_id for g in GIVEAWAYS.values() if g not in keep]:
        GIVEAWAYS.pop(gid, None)

    return {
        "version": 1,
        "saved_at": time.time(),
        "warnings": {
            str(uid): [[who, reason] for who, reason in entries[-MAX_WARNINGS_PER_USER:]]
            for uid, entries in WARNINGS.items() if entries
        },
        "giveaways": {
            str(gw.message_id): {
                "message_id": gw.message_id,
                "channel_id": gw.channel_id,
                "prize": gw.prize,
                "winner_count": gw.winner_count,
                "ends_at": gw.ends_at,
                "host_id": gw.host_id,
                "entrants": sorted(gw.entrants),
                "ended": gw.ended,
                "winners": gw.winners,
            }
            for gw in keep
        },
        "tickets": {
            str(t.channel_id): {
                "channel_id": t.channel_id,
                "guild_id": t.guild_id,
                "owner_id": t.owner_id,
                "opened_at": t.opened_at,
                "claimed_by": t.claimed_by,
                "status": t.status,
                "closed_at": t.closed_at,
                "closed_by": t.closed_by,
            }
            for t in TICKETS.values()
        },
    }


def save_data(reason: str = "") -> bool:
    """Write the JSON file atomically (temp file + replace = no corruption)."""
    global _dirty, _last_save, _save_errors
    try:
        payload = _serialize()
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, DATA_FILE)
        _dirty = False
        _last_save = time.time()
        _save_errors = 0
        return True
    except Exception as exc:
        _save_errors += 1
        print(f"⚠️ Could not save {DATA_FILE}: {exc}")
        return False


async def _flush_loop() -> None:
    """Background writer so button spam doesn't hammer the disk."""
    while not bot.is_closed():
        await asyncio.sleep(AUTOSAVE_SECONDS)
        if _dirty:
            save_data()


async def restore_state() -> None:
    """Load saved warnings/giveaways and restart any giveaway timers."""
    global _restored
    if _restored:
        return
    _restored = True

    if not os.path.exists(DATA_FILE):
        print(f"No {DATA_FILE} yet — starting with a clean slate.")
        return

    try:
        with open(DATA_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as exc:
        print(f"⚠️ {DATA_FILE} is unreadable ({exc}) — starting fresh, old file left alone.")
        return

    restored_warnings = 0
    for uid, entries in (raw.get("warnings") or {}).items():
        try:
            cleaned = [(str(w), str(r)) for w, r in entries]
            if cleaned:
                WARNINGS[int(uid)] = cleaned
                restored_warnings += 1
        except Exception:
            continue

    restored_gw = 0
    for _mid, g in (raw.get("giveaways") or {}).items():
        try:
            gw = Giveaway(
                message_id=int(g["message_id"]),
                channel_id=int(g["channel_id"]),
                prize=str(g.get("prize", "giveaway"))[:200],
                winner_count=max(1, int(g.get("winner_count", 1))),
                ends_at=float(g.get("ends_at", time.time())),
                host_id=int(g.get("host_id", 0)),
                entrants={int(x) for x in (g.get("entrants") or [])},
                ended=bool(g.get("ended")),
                winners=[int(x) for x in (g.get("winners") or [])],
            )
        except Exception:
            continue
        GIVEAWAYS[gw.message_id] = gw
        restored_gw += 1

    restored_tickets = 0
    for _cid, t in (raw.get("tickets") or {}).items():
        try:
            ticket = Ticket(
                channel_id=int(t["channel_id"]),
                guild_id=int(t["guild_id"]),
                owner_id=int(t.get("owner_id", 0)),
                opened_at=float(t.get("opened_at", time.time())),
                claimed_by=int(t.get("claimed_by", 0)),
                status=str(t.get("status", "open")),
                closed_at=float(t.get("closed_at", 0.0)),
                closed_by=int(t.get("closed_by", 0)),
            )
        except Exception:
            continue
        TICKETS[ticket.channel_id] = ticket
        restored_tickets += 1

    active = [g for g in GIVEAWAYS.values() if not g.ended]
    print(f"Restored {restored_warnings} warning record(s), {restored_gw} giveaway(s) "
          f"({len(active)} still running) and {restored_tickets} ticket(s) from {DATA_FILE}.")

    for gw in active:
        # Give the gateway a moment before fetching old messages
        asyncio.create_task(_resume_giveaway(gw))


async def _resume_giveaway(gw: Giveaway) -> None:
    await asyncio.sleep(3)
    await _gw_timer(gw)


@bot.hybrid_command(name="datainfo", aliases=["dbinfo"])
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def datainfo(ctx: commands.Context) -> None:
    """Show what's saved and where (admin only)."""
    active = sum(1 for g in GIVEAWAYS.values() if not g.ended)
    ended = len(GIVEAWAYS) - active
    entries = sum(len(g.entrants) for g in GIVEAWAYS.values())
    warns = sum(len(v) for v in WARNINGS.values())

    exists = os.path.exists(DATA_FILE)
    size = f"{os.path.getsize(DATA_FILE) / 1024:.1f} KB" if exists else "not created yet"
    saved = (
        f"{int(time.time() - _last_save)}s ago" if _last_save
        else ("on disk, unchanged this session" if exists else "—")
    )
    embed = info(
        f"**File:** `{os.path.abspath(DATA_FILE)}`\n"
        f"**Size:** {size}\n"
        f"**Last save:** {saved}\n"
        f"**Pending changes:** {'yes' if _dirty else 'no'}\n\n"
        f"**Warning records:** {len(WARNINGS)} ({warns} warnings)\n"
        f"**Giveaways:** {active} running, {ended} finished\n"
        f"**Total entries held:** {entries}\n"
        f"**Tickets:** {open_ticket_count()} open, "
        f"{sum(1 for t in TICKETS.values() if t.status == 'closed')} in history\n"
        f"**Transcripts:** `{os.path.abspath(TRANSCRIPT_DIR)}`",
        "💾 Data storage",
    )
    if _save_errors:
        embed.add_field(name="⚠️ Save errors", value=str(_save_errors), inline=False)
    await ctx.send(embed=embed)


# ===========================================================================
# 9. PERMISSION DIAGNOSTICS
# ===========================================================================

REQUIRED_PERMS: dict[str, str] = {
    "view_channel": "reading any channel",
    "send_messages": "every command",
    "embed_links": "all embeds (without it they show as plain text)",
    "read_message_history": "!purge, !snipe, giveaways",
    "add_reactions": "!poll, !notify confirmation",
    "manage_messages": "!purge, !lock, !unlock",
    "manage_channels": "the entire ticket system, !slowmode, !nuke",
    "ban_members": "!ban, !unban, !softban",
    "kick_members": "!kick",
    "moderate_members": "!timeout, !untimeout",
    "manage_roles": "!addrole, !removerole",
    "manage_nicknames": "!nick",
}


@bot.hybrid_command(name="permcheck", aliases=["setup", "perms"])
@commands.guild_only()
async def permcheck(ctx: commands.Context) -> None:
    """Check whether the bot has the permissions it needs on this server."""
    me = ctx.guild.me
    if me is None:
        await ctx.send(embed=err("I can't resolve my own member object here."))
        return

    perms = me.guild_permissions
    if perms.administrator:
        await ctx.send(embed=ok(
            "I have **Administrator** — every feature is unlocked.\n\n"
            "Just make sure my role is dragged **above** the roles I need to moderate "
            "(Server Settings → Roles), otherwise bans/kicks on higher members still fail.",
            "✅ Permissions look good",
        ))
        return

    missing = [(p, why) for p, why in REQUIRED_PERMS.items() if not getattr(perms, p, False)]
    if not missing:
        await ctx.send(embed=ok("All required permissions are present.", "✅ Permissions look good"))
        return

    lines = [f"`{p}` → {why}" for p, why in missing]
    embed = err("\n".join(lines)[:3900], f"❌ Missing {len(missing)} permission(s)")
    embed.set_footer(text="Fix: Server Settings → Roles → Synergy, or re-invite with Administrator")
    await ctx.send(embed=embed)

    # Role hierarchy warning
    above = [r.name for r in ctx.guild.roles
             if r.position > me.top_role.position and not r.is_default()]
    if above:
        await ctx.send(embed=info(
            "My role **" + me.top_role.name + "** sits below: " + ", ".join(above[:10]) +
            "\n\nModeration on those members will fail until you move my role higher "
            "(Server Settings → Roles → drag **" + me.top_role.name + "** up).",
            "⚠️ Role position",
        ))


# ===========================================================================
# 10. EVENTS
# ===========================================================================

@bot.event
async def on_ready() -> None:
    # Re-attach the persistent buttons so they keep working after a restart
    for view in (GiveawayView(), TicketPanelView(), TicketCloseView()):
        try:
            bot.add_view(view)
        except Exception as exc:
            print(f"Could not register view: {exc}")
    print("=" * 60)
    print(f"Logged in as {bot.user} (id: {getattr(bot.user, 'id', '?')})")
    print(f"Prefix: {PREFIX}   Commands loaded: {len(bot.commands)}")
    print(f"Watching handle: {WATCH_HANDLE}")
    print(f"Data file: {os.path.abspath(DATA_FILE)}")
    cached = sum(len(g.members) for g in bot.guilds)
    print(f"Members intent: {'on' if bot.intents.members else 'off'} — {cached} member(s) cached")
    print("=" * 60)

    # Load saved warnings/giveaways and resume any timers
    await restore_state()
    # Re-sync tickets against what actually exists in Discord
    try:
        await reconcile_tickets()
    except Exception as exc:
        print(f"Ticket reconciliation failed: {exc}")
    if not any(isinstance(t, asyncio.Task) and t.get_name() == "autosave" for t in asyncio.all_tasks()):
        task = asyncio.create_task(_flush_loop())
        task.set_name("autosave")

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands (may take up to an hour to appear).")
    except Exception as exc:
        print(f"Slash command sync skipped: {exc}")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    # AFK auto-clear
    if message.author.id in AFK and not message.content.startswith(PREFIX):
        reason = AFK.pop(message.author.id)
        try:
            await message.channel.send(
                f"👋 Welcome back {message.author.mention} — you were AFK: {reason}",
                delete_after=8,
            )
        except Exception:
            pass

    await bot.process_commands(message)


@bot.hybrid_command(name="echoslash")
async def echoslash(ctx: commands.Context, *, text: str = "hello") -> None:
    """Simple echo, handy for testing the prefix works."""
    await ctx.reply(text, mention_author=False)


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=err(f"Missing `{error.param.name}`.\nTry `{PREFIX}help {ctx.command.name}`."))
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=err(f"{error}\nTry `{PREFIX}help {ctx.command.name}`."))
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=err("You don't have permission to do that."))
        return
    if isinstance(error, commands.BotMissingPermissions):
        missing = ", ".join(error.missing_permissions)
        await ctx.send(embed=err(f"I'm missing permissions: `{missing}`."))
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(embed=err(f"Cooldown — try again in {error.retry_after:.1f}s."))
        return
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send(embed=err("That only works inside a server."))
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send(embed=err("You can't use that here."))
        return
    print(f"Unhandled error in {ctx.command}: {error!r}")
    try:
        await ctx.send(embed=err(f"Something broke: `{error}`"))
    except Exception:
        pass


# ===========================================================================
# HELP CATEGORIES  (also used by !help — keep names in sync with @bot.command)
# ===========================================================================

# Slash commands show the description to users, so strip the "Usage: !cmd ..."
# hint that only makes sense for the prefix version. The prefix help still
# shows it, because that reads from cmd.help rather than the app command.
for _cmd in bot.commands:
    _app = getattr(_cmd, "app_command", None)
    _desc = getattr(_app, "description", "") or ""
    if " Usage: !" in _desc:
        _app.description = _desc.split(" Usage: !")[0].strip()


CATEGORIES: dict[str, list[str]] = {
    "🔧 Core": ["help", "ping", "botinfo", "uptime", "invite", "membercount", "permcheck", "datainfo", "echoslash"],
    "🛡 Moderation": [
        "ban", "unban", "kick", "softban", "timeout", "untimeout", "warn", "warnings",
        "clearwarnings", "purge", "slowmode", "lock", "unlock", "nuke",
        "addrole", "removerole", "nick", "say", "announce",
    ],
    "ℹ️ Info": ["userinfo", "serverinfo", "avatar", "servericon", "roles", "channelinfo", "snipe"],
    "🧰 Utility": [
        "afk", "remind", "poll", "math", "b64", "password", "randomnumber",
        "uuidgen", "lmgtfy", "timestamp",
    ],
    "🎉 Fun": [
        "8ball", "roll", "coinflip", "choose", "rps", "joke", "dadjoke", "quote",
        "fact", "compliment", "roast", "ship", "rate", "iq", "gayrate", "reverse",
        "mock", "emojify", "clap", "scream", "spoiler", "hug", "pat", "slap",
        "highfive", "slots", "trivia", "riddle", "fortune", "numberbetween",
        "bofh", "owo", "tableflip", "unflip", "lenny", "vibecheck",
    ],
    f"📩 {WATCH_HANDLE}": ["notify", "watched"],
    "🎁 Giveaways": ["gstart", "gend", "greroll", "gentries"],
    "🎫 Tickets": ["ticketpanel", "tlist", "tclaim", "tclose", "tadd", "tremove"],
}


# ===========================================================================
# ENTRY POINT
# ===========================================================================

def main() -> None:
    if not TOKEN or TOKEN == "PASTE_YOUR_TOKEN_HERE":
        raise SystemExit(
            "\nNo token set.\n"
            "Either paste it into TOKEN at the top of bot.py, or run:\n"
            "  Windows:      set DISCORD_TOKEN=your_token\n"
            "  PowerShell:   $env:DISCORD_TOKEN=\"your_token\"\n"
            "  Linux/Mac:    export DISCORD_TOKEN=your_token\n"
        )

    def _on_term(signum, frame):
        # GitHub Actions / systemd send SIGTERM - flush before dying
        print(f"Signal {signum} received — saving data before exit...")
        save_data("shutdown")
        os._exit(0)

    for sig_name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _on_term)
            except Exception:
                pass

    try:
        bot.run(TOKEN, log_handler=None)
    finally:
        # Runs on Ctrl+C too
        save_data("shutdown")
        print(f"Saved to {os.path.abspath(DATA_FILE)}")


if __name__ == "__main__":
    main()
