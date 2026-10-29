// Synergy bot launcher — runs the real bot inside a GitHub Actions job.
//
// Why this file exists: the Creao GitHub App has no permission to write
// .github/workflows/*, but it can write ordinary files. The existing workflow
// already runs `node presence.js`, so this is the seam that lets the full bot
// run on a schedule without touching the workflow.
//
// What it does each job:
//   1. restores bot state (decrypted) from state.enc.json in the repo
//   2. creates a virtualenv and installs discord.py
//   3. runs bot.py in 20-minute slices, SIGTERM between them so the bot saves
//   4. re-encrypts the state and commits it, plus one dated snapshot per day
//   5. exits at 5h45m, just under GitHub's 6-hour job ceiling
//
// State is encrypted with AES-256-GCM using a key derived from DISCORD_TOKEN
// (already a repo secret), so commits are safe in a PUBLIC repo: user IDs,
// warnings and ticket transcripts never appear in plaintext in git history.
// If the token is rotated, old state can no longer be decrypted and the bot
// starts fresh — that is deliberate, not a bug.

const { spawn, spawnSync } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const TOKEN = process.env.DISCORD_TOKEN;
if (!TOKEN) {
  console.error('Missing DISCORD_TOKEN secret.');
  console.error('Repo -> Settings -> Secrets and variables -> Actions -> New repository secret.');
  console.error('Name must be exactly DISCORD_TOKEN, under Secrets (not Variables).');
  process.exit(1);
}

// The workflow only passes DISCORD_TOKEN; fill in defaults for the rest.
const DEFAULTS = { SUPPORT_ROLE_ID: '1564953055450112096' };
for (const [k, v] of Object.entries(DEFAULTS)) {
  if (!process.env[k]) process.env[k] = v;
}

const MAX_RUNTIME_MS = 5 * 60 * 60 * 1000 + 45 * 60 * 1000; // 5h45m
const SLICE_MS = 20 * 60 * 1000;
const VENV = '.venv';
const PY = path.join(VENV, 'bin', 'python');
const STATE_FILE = 'state.enc.json';
const BACKUP_DIR = 'backups';
const BACKUP_KEEP = 14;
const KEY_SALT = 'synergy-state-v1';
const startedAt = Date.now();

let child = null;
let stopping = false;
let repoIsPrivate = null;

const stamp = () => new Date().toISOString();
const elapsedMin = () => ((Date.now() - startedAt) / 60000).toFixed(1);

function run(cmd, args, opts = {}) {
  const res = spawnSync(cmd, args, { stdio: 'inherit', ...opts });
  if (res.error) throw res.error;
  if (res.status !== 0) throw new Error(`${cmd} exited with ${res.status}`);
  return res;
}

function git(args) {
  const res = spawnSync('git', args, { encoding: 'utf8' });
  return { ok: res.status === 0, out: `${res.stdout || ''}${res.stderr || ''}`.trim() };
}

// ---------------------------------------------------------------------------
// Encrypted state
// ---------------------------------------------------------------------------
function stateKey() {
  return crypto.scryptSync(TOKEN, KEY_SALT, 32);
}

function packState() {
  const paths = ['botdata.json', 'transcripts'].filter((p) => fs.existsSync(p));
  if (!paths.length) return null;
  const res = spawnSync('tar', ['-czf', '-', ...paths], { maxBuffer: 64 * 1024 * 1024 });
  if (res.status !== 0) return null;
  return res.stdout;
}

function unpackState(buf) {
  const res = spawnSync('tar', ['-xzf', '-'], { input: buf, maxBuffer: 64 * 1024 * 1024 });
  return res.status === 0;
}

function saveState() {
  const packed = packState();
  if (!packed) {
    console.log(`[${stamp()}] no local state to save yet`);
    return false;
  }
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', stateKey(), iv);
  const body = Buffer.concat([cipher.update(packed), cipher.final()]);
  fs.writeFileSync(
    STATE_FILE,
    JSON.stringify({
      v: 1,
      iv: iv.toString('base64'),
      tag: cipher.getAuthTag().toString('base64'),
      data: body.toString('base64'),
    })
  );
  console.log(`[${stamp()}] state encrypted -> ${STATE_FILE} (${body.length} bytes)`);
  return true;
}

function loadState() {
  if (!fs.existsSync(STATE_FILE)) {
    console.log(`[${stamp()}] no ${STATE_FILE} in checkout — starting fresh`);
    return false;
  }
  try {
    const obj = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
    const decipher = crypto.createDecipheriv('aes-256-gcm', stateKey(), Buffer.from(obj.iv, 'base64'));
    decipher.setAuthTag(Buffer.from(obj.tag, 'base64'));
    const packed = Buffer.concat([decipher.update(Buffer.from(obj.data, 'base64')), decipher.final()]);
    if (!unpackState(packed)) throw new Error('tar extraction failed');
    console.log(`[${stamp()}] state restored and decrypted from ${STATE_FILE}`);
    return true;
  } catch (e) {
    console.log(`[${stamp()}] could not restore state (${e.message}) — starting fresh`);
    return false;
  }
}

// ---------------------------------------------------------------------------
// Daily snapshots: one dated copy per day, newest 14 kept
// ---------------------------------------------------------------------------
function writeDailySnapshot() {
  try {
    if (!fs.existsSync(STATE_FILE)) return;
    fs.mkdirSync(BACKUP_DIR, { recursive: true });
    const today = new Date().toISOString().slice(0, 10);
    const target = `${BACKUP_DIR}/state-${today}.enc.json`;
    if (!fs.existsSync(target)) {
      fs.copyFileSync(STATE_FILE, target);
      console.log(`[${stamp()}] daily snapshot written: ${target}`);
    }
    const snaps = fs.readdirSync(BACKUP_DIR)
      .filter((f) => /^state-\d{4}-\d{2}-\d{2}\.enc\.json$/.test(f))
      .sort();
    for (const f of snaps.slice(0, Math.max(0, snaps.length - BACKUP_KEEP))) {
      fs.unlinkSync(`${BACKUP_DIR}/${f}`);
      console.log(`[${stamp()}] pruned old snapshot: ${f}`);
    }
  } catch (e) {
    console.log(`[${stamp()}] snapshot failed: ${e.message}`);
  }
}

async function checkVisibility() {
  if (repoIsPrivate !== null) return repoIsPrivate;
  const repo = process.env.GITHUB_REPOSITORY;
  const ghToken = process.env.GITHUB_TOKEN;
  if (!repo || !ghToken) return (repoIsPrivate = false);
  try {
    const res = await fetch(`https://api.github.com/repos/${repo}`, {
      headers: { Authorization: `Bearer ${ghToken}`, Accept: 'application/vnd.github+json', 'User-Agent': 'synergy-bot' },
    });
    const data = await res.json();
    repoIsPrivate = data.private === true;
    console.log(`[${stamp()}] repo visibility: ${repoIsPrivate ? 'private' : 'public'} (state is encrypted either way)`);
  } catch {
    repoIsPrivate = false;
  }
  return repoIsPrivate;
}

async function commitData() {
  if (!saveState()) return;
  await checkVisibility();

  git(['config', 'user.name', 'synergy-bot']);
  git(['config', 'user.email', 'actions@users.noreply.github.com']);
  writeDailySnapshot();
  git(['add', '-A', STATE_FILE, BACKUP_DIR]);

  if (git(['diff', '--cached', '--quiet']).ok) {
    console.log(`[${stamp()}] no state changes to commit`);
    return;
  }
  git(['commit', '-q', '-m', 'chore: save bot state [skip ci]']);
  git(['pull', '--rebase', '--autostash']);
  const pushed = git(['push']);
  console.log(`[${stamp()}] state ${pushed.ok ? 'committed and pushed' : 'commit failed to push'}`);
  if (!pushed.ok) console.log(pushed.out.split('\n').slice(-3).join('\n'));
}

// ---------------------------------------------------------------------------
// Bot process
// ---------------------------------------------------------------------------
function ensureDependencies() {
  if (!fs.existsSync(PY)) {
    console.log(`[${stamp()}] creating virtualenv`);
    run('python3', ['-m', 'venv', VENV]);
  }
  console.log(`[${stamp()}] installing requirements`);
  run(PY, ['-m', 'pip', 'install', '-q', '--disable-pip-version-check', '-r', 'requirements.txt']);
}

function runBot(sliceMs) {
  return new Promise((resolve) => {
    const sliceStart = Date.now();
    console.log(`[${stamp()}] starting bot (slice ${sliceMs / 60000} min)`);
    child = spawn(PY, ['-u', 'bot.py'], { stdio: 'inherit', env: process.env });

    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child = null;
      resolve(Date.now() - sliceStart);
    };

    const timer = setTimeout(() => {
      if (!child) return;
      console.log(`[${stamp()}] slice done — SIGTERM so the bot flushes its state`);
      try { child.kill('SIGTERM'); } catch { /* ignore */ }
      setTimeout(() => { if (child) { try { child.kill('SIGKILL'); } catch { /* ignore */ } } }, 30000);
    }, sliceMs);

    child.on('exit', (code, signal) => {
      console.log(`[${stamp()}] bot exited code=${code} signal=${signal}`);
      finish();
    });
    child.on('error', (e) => {
      console.error(`[${stamp()}] could not start bot: ${e.message}`);
      finish();
    });
  });
}

function shutdown(signal) {
  if (stopping) return;
  stopping = true;
  console.log(`[${stamp()}] ${signal} received — stopping bot`);
  if (child) { try { child.kill('SIGTERM'); } catch { /* ignore */ } }
  setTimeout(async () => {
    await commitData();
    process.exit(0);
  }, 8000);
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

// ---------------------------------------------------------------------------
(async () => {
  console.log(`[${stamp()}] synergy launcher starting`);
  try {
    ensureDependencies();
  } catch (e) {
    console.error(`[${stamp()}] dependency setup failed: ${e.message}`);
    process.exit(1);
  }

  loadState();

  let quickFailures = 0;
  while (!stopping && Date.now() - startedAt < MAX_RUNTIME_MS) {
    const duration = await runBot(SLICE_MS);
    if (stopping) break;

    quickFailures = duration < 45000 ? quickFailures + 1 : 0;
    if (quickFailures >= 3) {
      console.error('');
      console.error('Bot exited immediately 3 times. Likely causes:');
      console.error('  * DISCORD_TOKEN wrong or reset');
      console.error('  * Message Content Intent disabled in the Developer Portal');
      console.error('  * discord.py failed to install');
      console.error('');
      await commitData();
      process.exit(1);
    }

    await commitData();
    if (Date.now() - startedAt >= MAX_RUNTIME_MS) break;
    console.log(`[${stamp()}] restarting in 5s (${elapsedMin()}m elapsed)`);
    await new Promise((r) => setTimeout(r, 5000));
  }

  await commitData();
  console.log(`[${stamp()}] reached the runtime limit at ${elapsedMin()}m — next cron continues`);
  process.exit(0);
})();
