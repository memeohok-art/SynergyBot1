"""Synergy applications — creators apply with a channel handle, the owner accepts or
declines, and an accepted channel is handed to the comment bot.

Design notes
------------
* The verification code is derived from the handle with the same FNV-1a scheme the
  Synergy comment bot uses, so the code this bot shows always matches the one the
  verifier expects. Knowing the code proves nothing on its own — only editing the
  channel description does.
* On accept, the bot posts "<handle> <code>" into WELCOME_CHANNEL_ID. The comment
  bot polls that channel, checks the code is live in the channel description, and
  starts commenting. This bot never needs YouTube credentials.
* State lives in applications.json next to bot.py (override with APPLICATIONS_FILE).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Callable, Optional

import discord
from discord.ext import commands

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
APPS_FILE = os.getenv("APPLICATIONS_FILE", "applications.json")
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0") or 0)

_HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{2,40}$")
_URL_RE = re.compile(r"youtube\.com/@([A-Za-z0-9._-]{2,40})", re.I)


# ---------------------------------------------------------------- helpers

def verify_code(handle: str) -> str:
    """Deterministic per handle. Must stay in sync with token() in intake.js."""
    s = handle.lower().lstrip("@")
    h1 = 0x811C9DC5
    for ch in s:
        h1 ^= ord(ch)
        h1 = (h1 * 0x01000193) & 0xFFFFFFFF
    h2 = (0x01000193 ^ len(s)) & 0xFFFFFFFF
    for ch in reversed(s):
        h2 ^= ord(ch)
        h2 = (h2 * 0x811C9DC5) & 0xFFFFFFFF
    x = (h1 ^ (h2 >> 3)) & 0xFFFFFFFF
    out = []
    for i in range(6):
        out.append(ALPHABET[x % len(ALPHABET)])
        x = x // len(ALPHABET) + ((h2 >> (i + 1)) & 31)
    return "SYNERGY-" + "".join(out)


def normalise_handle(raw: str) -> Optional[str]:
    """Accept @name, name, or a youtube.com/@name URL."""
    if not raw:
        return None
    text = raw.strip()
    url = _URL_RE.search(text)
    if url:
        return "@" + url.group(1)
    text = text.lstrip("@").rstrip("/")
    if not _HANDLE_RE.match(text):
        return None
    return "@" + text


def _load() -> dict:
    try:
        with open(APPS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                data.setdefault("applications", {})
                return data
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ applications.json unreadable ({exc}) — starting empty, file left alone")
    return {"applications": {}}


def _save(data: dict) -> None:
    tmp = APPS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, APPS_FILE)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ could not save {APPS_FILE}: {exc}")


def _find(apps: dict, target: str):
    """Match an application by user id, @mention, or channel handle."""
    t = target.strip()
    if t.isdigit():
        return apps.get(t)
    mention = re.fullmatch(r"<@!?(\d+)>", t)
    if mention:
        return apps.get(mention.group(1))
    handle = normalise_handle(t)
    if handle:
        for app in apps.values():
            if app.get("handle", "").lower() == handle.lower():
                return app
    return None


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------- registration

def attach(
    bot: commands.Bot,
    *,
    prefix: str,
    owner_id: int,
    get_owner: Callable,
    ok: Callable[[str, str], discord.Embed],
    err: Callable[[str, str], discord.Embed],
    info: Callable[[str, str], discord.Embed],
    categories: dict,
) -> None:
    """Register the application commands on an existing bot."""

    def is_owner(user_id: int) -> bool:
        return bool(owner_id) and user_id == owner_id

    @bot.hybrid_command(name="apply", with_app_command=True)
    async def apply_cmd(ctx: commands.Context, handle: str) -> None:
        """Apply to have Synergy comment on your YouTube channel."""
        clean = normalise_handle(handle)
        if not clean:
            await ctx.send(
                embed=err(
                    f"`{handle}` doesn't look like a channel handle.\n"
                    f"Try `{prefix}apply @yourchannel` or `{prefix}apply youtube.com/@yourchannel`."
                )
            )
            return

        data = _load()
        apps = data["applications"]
        key = str(ctx.author.id)
        existing = apps.get(key)

        if existing and existing.get("status") == "accepted":
            await ctx.send(
                embed=info(
                    f"Your channel **{existing['handle']}** is already approved and in the rotation."
                )
            )
            return

        code = verify_code(clean)
        apps[key] = {
            "user_id": ctx.author.id,
            "user": str(ctx.author),
            "handle": clean,
            "code": code,
            "status": "pending",
            "applied_at": _stamp(),
        }
        _save(data)

        embed = discord.Embed(
            title="📥 Application received",
            colour=0x5EEAD4,
            description=(
                f"**Channel:** {clean}\n"
                f"**Status:** waiting for review\n\n"
                f"**One step to prove it's yours:** paste this code at the end of your "
                f"channel description in **YouTube Studio → Customisation → Basic info**:\n\n"
                f"### `{code}`\n\n"
                f"Then wait here. Once it's accepted, Synergy starts leaving one short comment "
                f"on each new video — and you can remove the code."
            ),
        )
        embed.set_footer(text=f"Code checked against {clean} automatically • {_stamp()}")
        await ctx.send(embed=embed)

    @bot.hybrid_command(name="apps", with_app_command=True)
    async def apps_cmd(ctx: commands.Context) -> None:
        """List pending channel applications (owner only)."""
        if not is_owner(ctx.author.id):
            await ctx.send(embed=err("Only the bot owner can review applications."))
            return

        data = _load()
        pending = [a for a in data["applications"].values() if a.get("status") == "pending"]
        if not pending:
            await ctx.send(embed=info("No pending applications right now."))
            return

        lines = []
        for a in sorted(pending, key=lambda x: x.get("applied_at", "")):
            lines.append(
                f"**{a['handle']}** — {a.get('user', '?')} (`{a['user_id']}`)\n"
                f"  code `{a['code']}` • {a.get('applied_at', '')}"
            )
        embed = discord.Embed(
            title=f"📥 {len(pending)} pending application(s)",
            colour=0x5EEAD4,
            description="\n\n".join(lines[:15]),
        )
        embed.set_footer(text=f"{prefix}accept <user|handle>  •  {prefix}decline <user|handle>")
        await ctx.send(embed=embed)

    @bot.hybrid_command(name="accept", with_app_command=True)
    async def accept_cmd(ctx: commands.Context, target: str) -> None:
        """Approve an application (owner only)."""
        if not is_owner(ctx.author.id):
            await ctx.send(embed=err("Only the bot owner can accept applications."))
            return

        data = _load()
        apps = data["applications"]
        app = _find(apps, target)
        if not app:
            await ctx.send(embed=err(f"No application found for `{target}`."))
            return
        if app.get("status") == "accepted":
            await ctx.send(embed=info(f"{app['handle']} was already accepted."))
            return

        app["status"] = "accepted"
        app["accepted_at"] = _stamp()
        _save(data)

        handoff = f"{app['handle']} {app['code']}"
        delivered = False
        if WELCOME_CHANNEL_ID:
            channel = bot.get_channel(WELCOME_CHANNEL_ID)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(WELCOME_CHANNEL_ID)
                except Exception:  # noqa: BLE001
                    channel = None
            if channel is not None:
                try:
                    await channel.send(
                        f"{handoff}\n*approved by {ctx.author} — code must be live in the "
                        f"channel description before comments start*"
                    )
                    delivered = True
                except Exception as exc:  # noqa: BLE001
                    print(f"⚠️ could not post handoff: {exc}")

        embed = ok(
            f"**{app['handle']}** approved.\n"
            + (
                "Handed to the comment bot in the welcome channel — it verifies the code, then starts commenting."
                if delivered
                else "⚠️ `WELCOME_CHANNEL_ID` is not set, so the comment bot was not notified. "
                f"Set it, or post this by hand: `{handoff}`"
            ),
            title="✅ Accepted",
        )
        await ctx.send(embed=embed)

        if app.get("user_id"):
            try:
                user = bot.get_user(app["user_id"]) or await bot.fetch_user(app["user_id"])
                if user:
                    await user.send(
                        f"Your channel **{app['handle']}** was approved. Synergy will leave one short "
                        f"comment on each new video. You can remove the verification code now."
                    )
            except Exception:  # noqa: BLE001
                pass

    @bot.hybrid_command(name="decline", with_app_command=True)
    async def decline_cmd(ctx: commands.Context, target: str) -> None:
        """Decline an application (owner only)."""
        if not is_owner(ctx.author.id):
            await ctx.send(embed=err("Only the bot owner can decline applications."))
            return

        data = _load()
        app = _find(data["applications"], target)
        if not app:
            await ctx.send(embed=err(f"No application found for `{target}`."))
            return

        app["status"] = "declined"
        app["declined_at"] = _stamp()
        _save(data)
        await ctx.send(embed=ok(f"**{app['handle']}** declined.", title="Declined"))

        if app.get("user_id"):
            try:
                user = bot.get_user(app["user_id"]) or await bot.fetch_user(app["user_id"])
                if user:
                    await user.send(
                        f"Your application for **{app['handle']}** was declined. "
                        f"You can apply again later with `{prefix}apply`."
                    )
            except Exception:  # noqa: BLE001
                pass

    for name in ("apply", "apps", "accept", "decline"):
        categories.setdefault("📥 Applications", [])
        if name not in categories["📥 Applications"]:
            categories["📥 Applications"].append(name)
