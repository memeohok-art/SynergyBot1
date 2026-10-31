/**
 * Synergy commenter — runs entirely on GitHub Actions, no CREAO dependency.
 *
 * Needs four repository secrets:
 *   YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN   (Google OAuth, one-time setup)
 *   DISCORD_TOKEN                                      (only for the optional ping)
 *
 * Optional:
 *   ANTHROPIC_API_KEY  — if present, comments are written by a model instead of templates
 *
 * State lives in state.json and is committed back by the workflow. Because the
 * workflow holds a concurrency lock, there is exactly one writer, so the split-state
 * duplication bug that hit the old setup cannot happen here.
 */
const fs = require('fs');
const path = require('path');

const HERE = __dirname;
const STATE_FILE = path.join(HERE, 'state.json');
const CONFIG_FILE = path.join(HERE, 'config.json');

const API = 'https://www.googleapis.com/youtube/v3';
const TOKEN_URL = 'https://oauth2.googleapis.com/token';

const CLIENT_ID = process.env.YT_CLIENT_ID || '';
const CLIENT_SECRET = process.env.YT_CLIENT_SECRET || '';
const REFRESH_TOKEN = process.env.YT_REFRESH_TOKEN || '';
const LLM_KEY = process.env.ANTHROPIC_API_KEY || '';

const DRY = process.env.DRY_RUN === '1';

// ---------------------------------------------------------------- state

const DEFAULT_STATE = { commented: {}, days: {}, recent: [], carry: {} };

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    return fallback;
  }
}

function loadState() {
  const s = readJson(STATE_FILE, null) || JSON.parse(JSON.stringify(DEFAULT_STATE));
  s.commented = s.commented || {};
  s.days = s.days || {};
  s.recent = s.recent || [];
  s.carry = s.carry || {};
  return s;
}

function saveState(s) {
  s.recent = s.recent.slice(-40);
  fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 1));
}

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function spentOn(state, handle, date = todayIso()) {
  return (state.days[date] || {})[handle] || 0;
}

function totalSpent(state, date = todayIso()) {
  return Object.values(state.days[date] || {}).reduce((a, b) => a + b, 0);
}

// ---------------------------------------------------------------- youtube auth

async function accessToken() {
  const body = new URLSearchParams({
    client_id: CLIENT_ID,
    client_secret: CLIENT_SECRET,
    refresh_token: REFRESH_TOKEN,
    grant_type: 'refresh_token',
  });
  const res = await fetch(TOKEN_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok || !json.access_token) {
    throw new Error(
      `token refresh failed (HTTP ${res.status}): ${json.error || 'unknown'}${
        json.error_description ? ' — ' + json.error_description : ''
      }`
    );
  }
  return json.access_token;
}

async function yt(pathname, token, params = {}) {
  const url = new URL(API + pathname);
  Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  const res = await fetch(url, { headers: { Authorization: 'Bearer ' + token } });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = (json.error && json.error.message) || JSON.stringify(json).slice(0, 160);
    const err = new Error(`YouTube ${pathname} HTTP ${res.status}: ${msg}`);
    err.status = res.status;
    throw err;
  }
  return json;
}

async function resolveChannel(handle) {
  const clean = handle.replace(/^@/, '');
  const res = await fetch(`https://www.youtube.com/@${clean}`, {
    headers: { 'User-Agent': 'Mozilla/5.0 (compatible; SynergyBot/1.0)', 'Accept-Language': 'en' },
    redirect: 'follow',
  });
  const html = await res.text();
  const id = (html.match(/"externalId":"(UC[A-Za-z0-9_-]{20,})"/) || [])[1];
  const title = (html.match(/<meta property="og:title" content="([^"]*)"/) || [])[1] || clean;
  if (!id) throw new Error(`could not resolve @${clean}`);
  return { channelId: id, title: title.replace(/&amp;/g, '&') };
}

async function listUploads(token, channelId, limit = 500) {
  const playlistId = 'UU' + channelId.slice(2);
  const out = [];
  let pageToken = '';
  for (let page = 0; page < 20; page++) {
    const json = await yt('/playlistItems', token, {
      part: 'snippet,contentDetails',
      playlistId,
      maxResults: 50,
      ...(pageToken ? { pageToken } : {}),
    });
    for (const item of json.items || []) {
      const vid = item.contentDetails && item.contentDetails.videoId;
      if (vid) {
        out.push({
          id: vid,
          title: (item.snippet && item.snippet.title) || '',
          publishedAt: (item.snippet && item.snippet.publishedAt) || '',
        });
      }
      if (out.length >= limit) return out;
    }
    pageToken = json.nextPageToken || '';
    if (!pageToken) break;
  }
  return out;
}

async function videoDetails(token, ids) {
  const json = await yt('/videos', token, {
    part: 'snippet,contentDetails,statistics',
    id: ids.join(','),
  });
  return json.items || [];
}

async function postComment(token, videoId, text) {
  const res = await fetch(API + '/commentThreads?part=snippet', {
    method: 'POST',
    headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      snippet: { videoId, topLevelComment: { snippet: { textOriginal: text } } },
    }),
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = (json.error && json.error.message) || JSON.stringify(json).slice(0, 180);
    const err = new Error(`comment insert HTTP ${res.status}: ${msg}`);
    err.status = res.status;
    throw err;
  }
  return json.id;
}

// ---------------------------------------------------------------- picking

/** Floors first, then remaining budget by catalogue size. Pure — unit tested. */
function computeQuotas(config, state, catalogSizes, dailyTotal) {
  const channels = config.channels || [];
  const base = Number(config.base_per_channel || zerosafe(config.default_floor, 3)) || 3;
  const mins = config.channel_min || {};

  const present = channels.filter((h) => (catalogSizes[h] || 0) > 0);
  const quotas = {};
  if (!present.length) return quotas;

  let used = 0;
  for (const h of present) {
    const want = Math.min(Number(mins[h] || base), catalogSizes[h]);
    quotas[h] = want;
    used += want;
  }
  // if floors exceed the day's budget, shrink proportionally
  if (used > dailyTotal && used > 0) {
    const factor = dailyTotal / used;
    used = 0;
    for (const h of present) {
      quotas[h] = Math.floor(quotas[h] * factor);
      used += quotas[h];
    }
  }
  // hand out what's left by catalogue size
  let pool = Math.max(0, dailyTotal - used);
  const totalCatalog = present.reduce((a, h) => a + catalogSizes[h], 0) || 1;
  const order = present
    .slice()
    .sort((a, b) => (catalogSizes[b] || 0) - (catalogSizes[a] || 0));
  for (const h of order) {
    if (pool <= 0) break;
    const share = Math.floor((pool * (catalogSizes[h] || 0)) / totalCatalog);
    const room = Math.max(0, catalogSizes[h] - (quotas[h] || 0));
    const add = Math.min(share, room);
    quotas[h] = (quotas[h] || 0) + add;
    pool -= add;
  }
  // any stragglers from rounding
  for (const h of order) {
    if (pool <= 0) break;
    const room = Math.max(0, catalogSizes[h] - (quotas[h] || 0));
    if (room > 0) {
      quotas[h] += 1;
      pool -= 1;
    }
  }

  // subtract what each channel already spent today
  for (const h of Object.keys(quotas)) {
    quotas[h] = Math.max(0, quotas[h] - spentOn(state, h));
  }
  return quotas;
}

function zerosafe(v, d) {
  return v == null ? d : v;
}

// ---------------------------------------------------------------- writing

const BANNED = [
  'vibe', 'aura', 'genuinely', 'sells it', 'respect for', 'the kind of',
  'lands clean', 'pacing', 'underrated', 'deserve',
];

// all-caps words that are adjectives/shouty, not names — they make bad noun phrases
const NOT_A_NAME = new Set([
  'CRAZIEST', 'BEST', 'FREE', 'NEW', 'LAST', 'TRUE', 'FIRST', 'TOP', 'ULTRA',
  'INSANE', 'EPIC', 'WILD', 'GOLD', 'CRAZY', 'HARD', 'EASY', 'FAST', 'SLOW',
  'BIG', 'SMALL', 'ONLY', 'MORE', 'MOST', 'WHO', 'WHAT', 'HOW', 'WHY', 'AND',
  'THE', 'NOT', 'ALL', 'MY', 'MYTHIC', 'LEGENDARY', 'RARE', 'SUPER', 'MEGA',
  'NEVER', 'AGAIN', 'PART', 'POV', 'LOL', 'OMG', 'VS', 'PT',
]);

function clean(text) {
  return text.replace(/[—–;]/g, ',').replace(/\s+/g, ' ').trim();
}

/** Template writer: short, casual, built from the video's own metadata. */
function templateComment(video, recent) {
  const title = (video.title || '').replace(/#\w+/g, '').trim();
  const secs = durationSeconds(video.duration) || null;
  const numbers = (title.match(/\b\d{1,3}\b/g) || []).filter((n) => Number(n) > 1 && Number(n) < 100);
  const caps = (title.match(/\b[A-Z][A-Z0-9']{2,}\b/g) || []).filter((w) => !NOT_A_NAME.has(w));
  const rawName = (caps[0] || '').toLowerCase();
  // "brocks" -> "brock", so templates can pluralise without doubling the s
  const singular = rawName.endsWith('s') && rawName.length > 3 ? rawName.slice(0, -1) : rawName;
  const plural = rawName.endsWith('s') ? rawName : rawName + 's';
  const name = singular;
  const num = numbers[0] || '';

  const lines = [];
  if (name && num) lines.push(`${num} ${plural} is chaos`);
  if (name && !num) lines.push(`that ${name} run was robbed`);
  if (num && !name) lines.push(`part ${num} no way`);
  if (secs) lines.push(`${secs} secs and still worth it`);
  lines.push('this one deserved more views');
  lines.push('no way that happened');
  lines.push('clean as always');
  lines.push('bro cooked on this one');
  lines.push('how is this so short');
  lines.push('had to rewatch that');

  const fresh = lines
    .map(clean)
    .filter((l) => l.length >= 6 && l.length <= 70)
    .filter((l) => !BANNED.some((b) => l.toLowerCase().includes(b)))
    .filter((l) => !recent.some((r) => r.toLowerCase() === l.toLowerCase()));

  if (!fresh.length) return null;
  // prefer lines that actually mention this video over generic reactions
  const specific = fresh.filter((l) => (name && l.includes(name)) || (num && l.includes(num)) || (secs && l.includes(String(secs))));
  const pool = specific.length && Math.random() < 0.7 ? specific : fresh;
  return pool[Math.floor(Math.random() * pool.length)];
}

function durationSeconds(iso) {
  if (!iso) return 0;
  const m = iso.match(/PT(?:(\d+)M)?(?:(\d+)S)?/);
  if (!m) return 0;
  return (Number(m[1] || 0) * 60) + Number(m[2] || 0);
}

/** Model-written comment when ANTHROPIC_API_KEY is set. Falls back to templates. */
async function llmComment(video) {
  if (!LLM_KEY) return null;
  const prompt =
    `Write ONE YouTube comment for this video. Rules: 3-9 words, one sentence, under 70 characters, ` +
    `lowercase is fine, no em dashes, no hashtags, no links, no emoji spam (max one), ` +
    `must reference something specific from the title/duration, sound like a real viewer, never say "keep going". ` +
    `Reply with the comment only.\n\nTitle: ${video.title}\nDuration: ${durationSeconds(video.duration)}s`;
  try {
    const res = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'x-api-key': LLM_KEY,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        model: 'claude-3-5-haiku-20241022',
        max_tokens: 60,
        messages: [{ role: 'user', content: prompt }],
      }),
    });
    const j = await res.json();
    const text = j && j.content && j.content[0] && j.content[0].text;
    if (!text) return null;
    const line = clean(text.split('\n')[0]).replace(/^["']|["']$/g, '');
    if (!line || line.length > 70) return null;
    if (BANNED.some((b) => line.toLowerCase().includes(b))) return null;
    return line;
  } catch (e) {
    console.log('LLM unavailable, using templates:', e.message);
    return null;
  }
}

// ---------------------------------------------------------------- main

async function main() {
  const config = readJson(CONFIG_FILE, null);
  if (!config) {
    console.error('config.json missing — copy config.example.json and edit it.');
    process.exit(1);
  }
  if (!CLIENT_ID || !CLIENT_SECRET || !REFRESH_TOKEN) {
    console.error(
      'Missing YouTube credentials. Add YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN ' +
        'as repository secrets (see GITHUB-COMMENTER.md).'
    );
    process.exit(1);
  }

  const state = loadState();
  const dailyTotal = Number(config.daily_total || 17);
  const maxPerRun = Number(config.max_per_run || 3);
  const delay = Math.max(30, Number(config.delay_seconds || 60));

  const alreadyToday = totalSpent(state);
  if (alreadyToday >= dailyTotal) {
    console.log(`Daily budget already used: ${alreadyToday}/${dailyTotal}. Nothing to do.`);
    return;
  }

  const token = await accessToken();
  console.log('YouTube token acquired.');

  // 1. resolve channels + catalogues
  const catalogues = {};
  const sizes = {};
  for (const handle of config.channels || []) {
    try {
      const ch = await resolveChannel(handle);
      const vids = await listUploads(token, ch.channelId);
      const fresh = vids.filter((v) => !state.commented[v.id]);
      catalogues[handle] = { ...ch, videos: fresh };
      sizes[handle] = fresh.length;
      console.log(`  ${handle} -> ${ch.title}: ${vids.length} videos, ${fresh.length} uncommented`);
    } catch (e) {
      console.log(`  ${handle}: skipped (${e.message})`);
      sizes[handle] = 0;
    }
  }

  // 2. quotas for the whole day, then take this run's slice
  const quota = computeQuotas(config, state, sizes, dailyTotal);
  const wanted = Object.entries(quota).filter(([, n]) => n > 0);
  if (!wanted.length) {
    console.log('Every channel is done for today.');
    return;
  }

  let budget = Math.min(maxPerRun, dailyTotal - alreadyToday);
  console.log(`Quotas: ${JSON.stringify(quota)} | this run takes up to ${budget}`);

  const picks = [];
  for (const [handle, n] of wanted.sort((a, b) => b[1] - a[1])) {
    let take = Math.min(n, budget - picks.length, catalogues[handle].videos.length);
    while (take-- > 0 && picks.length < budget) {
      const video = catalogues[handle].videos.shift();
      picks.push({ handle, ...catalogues[handle], video });
    }
    if (picks.length >= budget) break;
  }

  if (!picks.length) {
    console.log('Nothing picked — queue may be empty.');
    return;
  }

  // 3. details for context
  const details = await videoDetails(token, picks.map((p) => p.video.id));
  const byId = new Map(details.map((d) => [d.id, d]));

  // 4. post
  let posted = 0;
  for (let i = 0; i < picks.length; i++) {
    const p = picks[i];
    const meta = byId.get(p.video.id) || {};
    const video = {
      id: p.video.id,
      title: (meta.snippet && meta.snippet.title) || p.video.title,
      duration: (meta.contentDetails && meta.contentDetails.duration) || '',
    };

    let text = await llmComment(video);
    if (!text) text = templateComment(video, state.recent);
    if (!text) {
      console.log(`  ${video.id}: no acceptable text, skipping`);
      continue;
    }

    if (DRY) {
      console.log(`  [dry] ${p.handle} ${video.id} :: ${text}`);
      continue;
    }

    try {
      const id = await postComment(token, video.id, text);
      state.commented[video.id] = {
        title: video.title,
        text,
        handle: p.handle,
        commentId: id,
        at: new Date().toISOString(),
      };
      const d = todayIso();
      state.days[d] = state.days[d] || {};
      state.days[d][p.handle] = (state.days[d][p.handle] || 0) + 1;
      state.recent.push(text);
      saveState(state);
      posted++;
      console.log(`  posted ${p.handle} ${video.id} :: ${text}`);
    } catch (e) {
      console.log(`  failed ${video.id}: ${e.message}`);
      if (e.status === 403 || e.status === 401) {
        console.log('  auth/quota problem — stopping this run.');
        break;
      }
    }

    if (i < picks.length - 1) await new Promise((r) => setTimeout(r, delay * 1000));
  }

  console.log(`Done. Posted ${posted}/${picks.length} this run. Today: ${totalSpent(state)}/${dailyTotal}.`);
}

if (require.main === module) {
  main().catch((e) => {
    console.error('fatal:', e.message);
    process.exit(1);
  });
}

module.exports = { computeQuotas, templateComment, durationSeconds, clean };
