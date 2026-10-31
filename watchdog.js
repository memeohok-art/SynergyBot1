/**
 * Synergy watchdog — DMs you on Discord when the comment bot has gone quiet.
 *
 * It reads heartbeat.json, which the comment bot updates after every posting run.
 * If the newest "last_comment_at" is older than the threshold, you get one DM.
 * A cooldown in watchdog-state.json stops it nagging every hour.
 *
 * Env:
 *   DISCORD_TOKEN      bot token (same one the presence job uses)
 *   WATCHDOG_USER_ID   who to DM (your Discord user ID)
 *   STALE_HOURS        optional, default 12
 *   COOLDOWN_HOURS     optional, default 6
 *   DRY_RUN            optional, "1" to log without sending
 */
const fs = require('fs');
const path = require('path');

const API = 'https://discord.com/api/v10';
const TOKEN = (process.env.DISCORD_TOKEN || '').trim();
const USER_ID = (process.env.WATCHDOG_USER_ID || '').trim();
const STALE_HOURS = Number(process.env.STALE_HOURS || 12);
const COOLDOWN_HOURS = Number(process.env.COOLDOWN_HOURS || 6);
const DRY = process.env.DRY_RUN === '1';

const HERE = __dirname;
const HEARTBEAT = path.join(HERE, 'heartbeat.json');
const STATE = path.join(HERE, 'watchdog-state.json');

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    return fallback;
  }
}

function hoursSince(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return (Date.now() - t) / 3600000;
}

async function api(pathname, opts = {}) {
  const res = await fetch(API + pathname, {
    ...opts,
    headers: {
      Authorization: 'Bot ' + TOKEN,
      'Content-Type': 'application/json',
      ...(opts.headers || {}),
    },
  });
  const text = await res.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch (e) {
    body = { raw: text.slice(0, 200) };
  }
  return { status: res.status, ok: res.ok, body };
}

async function dm(content) {
  const ch = await api('/users/@me/channels', {
    method: 'POST',
    body: JSON.stringify({ recipient_id: USER_ID }),
  });
  if (!ch.ok) {
    console.log(`could not open DM channel (HTTP ${ch.status}): ${JSON.stringify(ch.body).slice(0, 160)}`);
    return false;
  }
  const sent = await api(`/channels/${ch.body.id}/messages`, {
    method: 'POST',
    body: JSON.stringify({ content }),
  });
  if (!sent.ok) {
    console.log(`could not send DM (HTTP ${sent.status}): ${JSON.stringify(sent.body).slice(0, 160)}`);
    return false;
  }
  return true;
}

(async () => {
  if (!TOKEN) {
    console.log('Missing DISCORD_TOKEN — add it in Settings → Secrets and variables → Actions.');
    return;
  }
  if (!USER_ID) {
    console.log('Missing WATCHDOG_USER_ID — set it to your Discord user ID (a repository variable is fine).');
    return;
  }

  const hb = readJson(HEARTBEAT, null);
  const state = readJson(STATE, { lastAlertAt: null });

  if (!hb) {
    console.log('No heartbeat.json in the repo yet — the comment bot has not reported in.');
    console.log('Treating that as stale only if a threshold is configured; nothing to compare.');
    return;
  }

  const ageComments = hoursSince(hb.last_comment_at);
  const ageRun = hoursSince(hb.last_run_at);
  console.log(`last comment: ${hb.last_comment_at || 'never'} (${ageComments === null ? 'unknown' : ageComments.toFixed(1) + 'h ago'})`);
  console.log(`last run    : ${hb.last_run_at || 'never'} (${ageRun === null ? 'unknown' : ageRun.toFixed(1) + 'h ago'})`);
  console.log(`posted today: ${hb.posted_today ?? '?'} / ${hb.daily_total ?? '?'} | queued: ${hb.queued ?? '?'}`);
  console.log(`threshold: ${STALE_HOURS}h | cooldown: ${COOLDOWN_HOURS}h`);

  const stale = ageComments === null || ageComments > STALE_HOURS;
  if (!stale) {
    console.log('Healthy — nothing to do.');
    return;
  }

  const ageCooldown = hoursSince(state.lastAlertAt);
  if (ageCooldown !== null && ageCooldown < COOLDOWN_HOURS) {
    console.log(`Still stale, but last alert was ${ageCooldown.toFixed(1)}h ago — within cooldown, staying quiet.`);
    return;
  }

  const headline =
    ageComments === null
      ? '⚠️ Synergy has no recorded comment in the heartbeat file.'
      : `⚠️ Synergy has not commented in ${ageComments.toFixed(0)} hours.`;

  const detail = [
    headline,
    '',
    `Last comment: ${hb.last_comment_at || 'never'}`,
    `Last run: ${hb.last_run_at || 'never'}`,
    hb.posted_today != null ? `Today: ${hb.posted_today}/${hb.daily_total ?? '?'} posted` : '',
    hb.queued != null ? `Queue: ${hb.queued} videos left` : '',
    hb.note ? `Note: ${hb.note}` : '',
    '',
    'Check the CREAO bot run and the YouTube connection.',
  ]
    .filter(Boolean)
    .join('\n');

  console.log('--- alert ---');
  console.log(detail);

  if (DRY) {
    console.log('DRY_RUN set — not sending.');
    return;
  }

  if (await dm(detail)) {
    console.log('DM sent.');
    state.lastAlertAt = new Date().toISOString();
    fs.writeFileSync(STATE, JSON.stringify(state, null, 1));
    console.log('cooldown stamped in watchdog-state.json');
  } else {
    process.exitCode = 1;
  }
})();
