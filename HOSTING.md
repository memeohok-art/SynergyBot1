# 24/7 hosting (systemd)

Runs the bot continuously on a Linux box — no 6-hour job limits, no cold starts,
no gaps. Restarts automatically if it ever crashes, and starts on boot.

Same `bot.py`, no code changes. GitHub Actions becomes optional.

---

## 1. Pick a host

| Option | Cost | Notes |
|---|---|---|
| **Oracle Cloud Always Free** | $0 | ARM Ampere or AMD micro VM, 1 GB RAM is plenty. Most popular free option; card required for verification |
| **Hetzner CX22** | ~€4/mo | Fastest, most reliable for the money |
| **DigitalOcean / Vultr** | ~$4–6/mo | Easiest UI, 1 GB droplet |
| **Raspberry Pi / old laptop** | $0 | Works great; needs a stable internet + power |

Any Ubuntu 22.04/24.04 or Debian 12 image works. 1 vCPU + 1 GB RAM + 10 GB disk
is more than enough — the bot idles around 60–90 MB.

## 2. Install (one command)

SSH in, then:

```bash
curl -sL https://raw.githubusercontent.com/memeohok-art/SynergyBot1/main/install.sh -o install.sh
sudo bash install.sh
```

That script will:

1. install `python3`, `python3-venv`, `git`
2. create a system user `synergy` (no shell, no login)
3. clone the repo to `/opt/synergy-bot`
4. build a virtualenv and install `discord.py`
5. create `/var/lib/synergy-bot` for `botdata.json` + `transcripts/`
6. create `/opt/synergy-bot/.env` (mode `600`) and install the systemd unit
7. `systemctl enable synergy-bot`

It's idempotent — re-running it updates code and dependencies without touching
your token or your data.

## 3. Add the token

```bash
sudo nano /opt/synergy-bot/.env
```

Set:

```
DISCORD_TOKEN=your_bot_token_here
SUPPORT_ROLE_ID=1564953055450112096
```

Save with `Ctrl+O`, `Enter`, `Ctrl+X`. Then:

```bash
sudo systemctl restart synergy-bot
journalctl -u synergy-bot -f
```

A healthy start looks like:

```
Logged in as Synergy (id: 1564959649482940526)
Prefix: !   Commands loaded: 93
Data file: /var/lib/synergy-bot/botdata.json
```

`Ctrl+C` stops the log view — it does **not** stop the bot.

## 4. Turn off GitHub Actions (important!)

Two copies of the bot on the same token will kick each other off the gateway.

In the repo: **Actions → synergy-bot → ⋯ → Disable workflow**
(and disable `synergy-presence` too if it still exists), or simply delete
`.github/workflows/presence.yml` in the web UI.

## 5. Daily use

```bash
sudo systemctl status synergy-bot      # is it up?
sudo systemctl restart synergy-bot     # restart
sudo systemctl stop synergy-bot        # stop
journalctl -u synergy-bot -f           # live log
journalctl -u synergy-bot -n 100       # last 100 lines
journalctl -u synergy-bot --since "1 hour ago"
```

**Update to the latest code:**

```bash
cd /opt/synergy-bot && sudo bash deploy.sh
```

**Back up everything:**

```bash
sudo tar czf ~/synergy-backup-$(date +%F).tar.gz /var/lib/synergy-bot /opt/synergy-bot/.env
```

Copy it off the server. That tarball has your warnings, tickets, transcripts and token.

---

## Why this unit file is shaped this way

| Setting | Reason |
|---|---|
| `Restart=always` + `RestartSec=5` | Crashes, OOM kills and network drops all self-heal within 5 seconds |
| `KillSignal / SIGTERM` + `TimeoutStopSec=30` | `bot.py` catches SIGTERM, writes `botdata.json` atomically, then exits — so a restart never loses data |
| `StateDirectory=synergy-bot` | Creates `/var/lib/synergy-bot` with correct ownership, and it stays writable even under `ProtectSystem=strict` |
| `ProtectSystem=strict` + `ProtectHome` | Filesystem is read-only except the data dir; a compromised dependency can't rewrite the system |
| `User=synergy` (no shell) | Not root. If the bot is ever exploited, the blast radius is one unprivileged user |
| `EnvironmentFile=.env` (mode 600) | The token never appears in the unit file, in `ps`, or in shell history |
| `network-online.target` | Avoids a reconnect storm if the bot starts before DNS is up |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `status` says `activating (auto-restart)` | `journalctl -u synergy-bot -n 50` — almost always a bad token or missing `discord.py` |
| `No token set` | The `.env` has `DISCORD_TOKEN=` empty, or extra spaces/quotes around the value |
| `PrivilegedIntentsRequired` | Turn on **Message Content Intent** in the Discord Developer Portal |
| Bot online, commands ignored | Same thing — Message Content Intent is off |
| `Permission denied` on `botdata.json` | `sudo chown -R synergy:synergy /var/lib/synergy-bot` |
| Changes to `.env` not applied | `.env` is only read at startup — `sudo systemctl restart synergy-bot` |
| Bot dies after ~5 hours | You're still running the GitHub Actions workflow too — disable it |

## Migrating from Actions

The Actions cache isn't accessible from the server, so warnings, active
giveaways and ticket history start empty. Tickets themselves survive anyway —
their channels still carry the `ticket-owner:<id>` topic, so the bot re-imports
them on first boot (`+N recovered` in the log).
