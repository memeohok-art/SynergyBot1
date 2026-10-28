# Synergy on GitHub — free hosting

Two free pieces, both on GitHub, no credit card:

| Piece | What it does | Where |
|---|---|---|
| **Presence keeper** | Keeps the Discord bot showing **online** | GitHub Actions |
| **Dashboard** | Hosts `synergy.html` publicly | GitHub Pages |

## What this does NOT do
It does not comment on YouTube videos and does not admit channels. That work stays on the CREAO
bot, because it needs the YouTube connection and the shared state file. This repo is the free
hosting layer for the two things GitHub is actually good at.

## Setup (about 5 minutes)

### 1. Create the repo
Create a new repository (public is fine and costs nothing) and upload this folder's contents,
keeping the `.github/` folder — that's where the workflows live.

### 2. Add the bot token as a secret
Repo → **Settings → Secrets and variables → Actions → New repository secret**

- Name: `DISCORD_TOKEN`
- Value: your bot token (the same value you saved in the platform's secrets)

The workflow reads it as `${{ secrets.DISCORD_TOKEN }}`. It is never written to a file.

### 3. Turn on the presence job
**Actions → synergy-presence → Run workflow.** The first run should log:

```
logged in as Synergy#0712
servers: Synergy
presence online
```

The green dot appears in the member list within seconds.

### 4. Turn on Pages
**Settings → Pages → Source: GitHub Actions.** Then push any change under `site/` (or run the
`synergy-pages` workflow). Your dashboard lands at:

```
https://<your-username>.github.io/<repo>/synergy.html
```

## How the presence stays up, and where it doesn't
GitHub caps a job at **6 hours**. So `presence.js` holds the connection for **5h45m**, then exits
cleanly, and the cron (`0 */5 * * *`) starts the next run. Because each run lasts longer than the
5-hour gap, runs overlap and the dot stays on almost continuously.

**The honest gaps:**
- Between the old job ending and the new one starting there is usually **1–5 minutes grey**. GitHub's
  scheduler is not precise and can be several minutes late under load.
- Scheduled workflows are **disabled automatically after 60 days of repository inactivity**. Any
  commit, or opening the Actions tab now and then, resets that clock.
- Private repos bill Actions minutes (2,000/month free). A 5h45m job every 5 hours burns roughly
  700 minutes a day, so **keep this repo public** — public repos run free with no minute limit.

## Cost
GitHub Pages: free for public repos. Actions: free for public repos. Discord: free. Total: £0.

If you want a dot that never blinks, that needs a real always-on host (a small VPS, Railway, a Pi) —
GitHub's free tier trades a few grey minutes for the price.
