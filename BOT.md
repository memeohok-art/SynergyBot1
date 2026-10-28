# THEMASTEROOGWAY BOT

One file, no database, no extra services. **93 commands**: moderation, info, utility, fun, giveaways and tickets — plus the `!notify` watcher that DMs you whenever someone mentions the `@themasteroogway_` handle.

Warnings, giveaways **and tickets** are saved to **`botdata.json`**, so they survive restarts, crashes and reboots.

## Setup (3 steps)

**1. Install**
```bash
pip install -r requirements.txt
```

**2. Add your token** — either paste it into `TOKEN` at the top of `bot.py`, put it in a `.env` file next to it, or set an env var:
```bash
# .env  (loaded automatically)
DISCORD_TOKEN=your_token_here

# Windows PowerShell
$env:DISCORD_TOKEN="your_token_here"
# Linux / macOS
export DISCORD_TOKEN=your_token_here

python bot.py
```

**3. Turn on the intent** — Developer Portal → your app → **Bot** → *Privileged Gateway Intents* → **MESSAGE CONTENT INTENT: ON**.

Without that, `!notify hello @themasteroogway_` arrives as an empty string and nothing ever matches.

Invite the bot with scopes `bot` + `applications.commands`, and give it **Administrator** (or at minimum: Ban Members, Kick Members, Moderate Members, Manage Messages, Manage Roles, Manage Channels, Manage Nicknames, Read/Send Messages, Embed Links, Add Reactions).

### Optional settings

| Env var | Default | What it does |
|---|---|---|
| `TOKEN` / `DISCORD_TOKEN` | — | Bot token |
| `PREFIX` | `!` | Command prefix |
| `WATCH_HANDLE` | `themasteroogway_` | YouTube handle to watch |
| `OWNER_ID` | `0` | Who gets DMed. `0` = the app's owner |
| `LOG_CHANNEL_ID` | `0` | Channel for failed DMs / mod logs |
| `COOLDOWN_SECONDS` | `20` | Per-user anti-spam for `!notify` |
| `SUPPORT_ROLE_ID` | `0` | Role that can see every ticket. `0` = anyone with Manage Channels |
| `TICKET_CATEGORY_ID` | `0` | Category new ticket channels are created in |
| `DATA_FILE` | `botdata.json` | Where warnings/giveaways are saved |
| `AUTOSAVE_SECONDS` | `3` | How often pending changes hit the disk |
| `TRANSCRIPT_DIR` | `transcripts` | Folder ticket transcripts are written to |

---

## The `!notify` command

DMs you the **username + the text they typed** whenever the message contains the handle. All of these work:

```
!notify new video from @themasteroogway_
!notify check the master oogway latest upload
!notify youtube.com/@themasteroogway_/videos
!notify THEMASTEROOGWAY just dropped a short
!notify the-master-oogway collab when
```

`_`, `-`, spaces and dots between letters are ignored, and `@` is optional. No handle → no DM, so it can't be spammed by accident. One DM per user every 20 seconds.

`!watched` shows what's being watched. `!notify` with no text shows usage.

---

## All 93 commands

**🔧 Core** — `help` `ping` `botinfo` `uptime` `invite` `membercount` `permcheck` `datainfo` `echoslash`

**🛡 Moderation** — `ban` `unban` `kick` `softban` `timeout` (`mute`) `untimeout` (`unmute`) `warn` `warnings` `clearwarnings` `purge` (`clear`) `slowmode` `lock` `unlock` `nuke` `addrole` `removerole` `nick` `say` `announce`

**ℹ️ Info** — `userinfo` (`whois`) `serverinfo` `avatar` `servericon` `roles` `channelinfo` `snipe`

**🧰 Utility** — `afk` `remind` `poll` `math` (`calc`) `b64` `password` `randomnumber` `uuidgen` `lmgtfy` `timestamp`

**🎉 Fun** — `8ball` `roll` `coinflip` `choose` `rps` `joke` `dadjoke` `quote` `fact` `compliment` `roast` `ship` `rate` `iq` `gayrate` `reverse` `mock` `emojify` `clap` `scream` `spoiler` `hug` `pat` `slap` `highfive` `slots` `trivia` `riddle` `fortune` `numberbetween` `bofh` `owo` `tableflip` `unflip` `lenny` `vibecheck`

**📩 the-master-oogway** — `notify` `watched`

**🎁 Giveaways** — `gstart` `gend` `greroll` `gentries`

**🎫 Tickets** — `ticketpanel` `tlist` `tclaim` `tclose` `tadd` `tremove`

`!help` lists them all, `!help ban` explains one.

---

## 🎁 Giveaways

```
!gstart 10m 1 Nitro Classic          time, winner count, prize
!gstart 2h 3 Steam key               multiple winners
!gend 1234567890123456789            end early by message ID
!gend                                or end the newest one in this channel
!greroll 1234567890123456789         pick new winners (never repeats an old one)
!gentries 1234567890123456789        who entered (staff only)
```

Members press **Enter** on the giveaway message — pressing it again removes their entry. The message updates its entry count live and ends itself on time. Winners are pinged in the channel. Multi-word prizes work, so `!gstart 1h 2 Discord Nitro 3 months` takes the whole ending as the prize. Needs **Manage Server** to create giveaways.

Buttons keep working after a restart, and **entry lists are saved to disk** — restart the bot mid-giveaway and it resumes the countdown and ends on schedule.

---

## 🎫 Ticket system

One-time setup:

1. `!ticketpanel` in your support channel — posts the panel with the **Open a ticket** button
2. Optional but recommended: set `SUPPORT_ROLE_ID` so your staff role can see tickets

What happens when a member clicks it:

- A **private channel** `#ticket-username` is created
- `@everyone` can't see it; only the opener, the bot, and your support role can
- Staff get pinged, the opener gets a greeting plus a **Close ticket** button
- One open ticket per person — clicking again just links their existing ticket
- **Close ticket** (or `!tclose`) saves a transcript, then deletes the channel after 5 seconds

Staff commands: `!tlist` (every open ticket with owner, age and who claimed it), `!tclaim` (mark it as yours), `!tadd @user`, `!tremove @user`.

The bot needs **Manage Channels** for all of this.

### Tickets survive restarts — every time

Three independent layers, so nothing is ever lost:

1. **Topic marker** — every ticket channel's topic holds `ticket-owner:<id>`. Discord itself remembers this, so it survives even a total wipe of the bot's files.
2. **Registry in `botdata.json`** — opener, open time, who claimed it, and closed history are written to disk the moment a ticket is created.
3. **Reconciliation on every login** — at startup the bot walks the server's channels and re-imports any ticket it finds, marks tickets whose channels are gone as closed, and prunes old history. Miss a write, lose the file, restart mid-ticket: it rebuilds itself.

### Transcripts

When a ticket closes, the full conversation (with attachment links) is written to `transcripts/ticket-name-<timestamp>.txt` **before** the channel is deleted. If `LOG_CHANNEL_ID` is set, the file is posted there too — so a closed ticket is archived, not erased.

---

## Examples

```
!ban @spammer raiding with invites     kick/ban work with a reason
!timeout @user 10m spamming            durations: 30s 10m 2h 1d
!purge 50                              deletes 50 messages
!lock  /  !unlock                      shut or open the channel
!addrole @user Member                  give a role
!remind 20m check the stream
!poll Movie night? | Friday | Saturday
!roll 2d20       !8ball will I hit 1k subs?
!ship @you @friend
!trivia          respond in chat within 15s
!gstart 30m 1 Nitro    !gend    !greroll
!ticketpanel                           post the support panel
```

## 💾 What gets saved

`botdata.json` sits next to `bot.py` and holds:

- **Warnings** — who warned whom, and why (`!warn`, `!warnings`, `!clearwarnings`)
- **Giveaways** — prize, winner count, end time, every entrant, and past winners so `!greroll` still works after a reboot
- **Tickets** — channel, opener, open time, who claimed it, and closed history

Transcripts go to `transcripts/` as plain `.txt` files.

How it stays safe:

- Written to `botdata.json.tmp` first, then swapped in — a crash mid-write leaves the old file intact, never a half-written one
- **SIGTERM / Ctrl+C** trigger a final save before exit
- A corrupt or hand-edited file is ignored with a warning instead of crashing the bot
- Keeps the last 50 finished giveaways and the last 50 warnings per user, so the file can't grow forever

`!datainfo` (admin) shows the path, size, last save time, and what's stored.

Back it up by copying the file. Reset everything by deleting it while the bot is stopped.

## Notes / limits
- **Warnings and giveaway entries are saved to `botdata.json`** (next to `bot.py`) — atomic writes, so a crash mid-write can't corrupt it. Warnings save the instant they're issued; giveaway entries flush within ~3 seconds.
- `!purge` deletes up to 200 at once (Discord's bulk-delete limit).
- Timeouts are capped at 28 days by Discord; `!slowmode` at 6 hours.
- Moderation commands refuse to act on the server owner, on you, on the bot itself, or on anyone whose top role is above the bot's own role.
- `github-workflow-bot.yml` in this folder is optional — only needed if you want to host it on GitHub Actions instead of your own machine.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot online but commands do nothing | Message Content Intent is off |
| "I'm missing permissions" | Bot role is below the target's role, or a permission is unchecked |
| `!notify` says nothing was sent | The text didn't contain the handle |
| DM never arrives | Turn on **Privacy Settings → Direct Messages** for the server |
| `PrivilegedIntentsRequired` crash | Enable Message Content Intent in the portal |
| Ticket button says missing permission | Give the bot **Manage Channels** |
| Giveaway buttons dead after restart | They're re-registered on login; the console prints "Restored … giveaway(s)" |

**Security:** never commit the token. If it leaks, hit *Reset Token* in the Developer Portal immediately — a leaked token is full control of your bot.
