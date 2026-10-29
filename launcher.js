// Synergy bot launcher — runs the real 93-command bot inside a GitHub Actions job.
//
// This replaces the old "presence keeper" (which only held a websocket so the
// bot showed as online). It now installs the Python dependencies and runs
// bot.py, which provides every command AND keeps the connection.
//
// Why a launcher instead of a pure Python workflow: the Creao GitHub App is not
// granted GitHub's "Workflows" permission, so .github/workflows/*.yml cannot be
// written from outside. The existing workflow already runs `node presence.js`,
// so this file is the seam that lets the bot run with zero workflow edits.
//
// Behaviour:
//   * data is committed back to the repo every 20 minutes — but ONLY if the repo
//     is private, because botdata.json holds user IDs, warnings and ticket
//     transcripts and must never land in a public repo
//   * the bot is stopped with SIGTERM, which is what makes it save its state
//   * exits cleanly at 5h45m, just under GitHub's 6 hour job ceiling
//   * restarts the bot if it crashes, but gives up after 3 instant failures so a
//     bad token doesn't spin for five hours

const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const TOKEN = process.env.DISCORD_TOKEN;
if (!TOKEN) {
  console.error('Missing DISCORD_TOKEN secret.');
  console.error('Repo -> Settings -> Secrets and variables -> Actions -> New repository secret.');
  console.error('Name must be exactly DISCORD_TOKEN, and it must be under Secrets (not Variables).');
  process.exit(1);
}

// The workflow only passes DISCORD_TOKEN. Fill in sensible defaults so tickets
// ping staff and notifications reach you. A real secret/env var always wins.
const DEFAULTS = {
  SUPPORT_ROLE_ID: '1564953055450112096', // Staff role in the Synergy server
};
for (const [key, value] of Object.entries(DEFAULTS)) {
  if (!process.env[key]) process.env[key] = value;
}

const MAX_RUNTIME_MS = 5 * 60 * 60 * 1000 + 45 * 60 * 1000; // 5h45m
const SLICE_MS = 20 * 60 * 1000;                            // restart + save every 20 min
const VENV = '.venv';
const PY = path.join(VENV, 'bin', 'python');
const startedAt = Date.now();

let child = null;
let stopping = false;
let committedOnce = false;
let repoIsPrivate = false;
let visibilityChecked = false;

const stamp = () => new Date().toISOString();

function run(cmd, args, opts = {}) {
  const res = spawnSync(cmd, args, { stdio: 'inherit', ...opts });
  if (res.error) throw res.error;
  if (res.status !== 0) throw new Error(`${cmd} exited with ${res.status}`);
  return res;
}

function elapsedMin() {
  return ((Date.now() - startedAt) / 60000).toFixed(1);
}

// ---------------------------------------------------------------------------
// Dependency setup
// ---------------------------------------------------------------------------
function ensureDependencies() {
  if (!fs.existsSync(PY)) {
    console.log(`[${stamp()}] creating virtualenv`);
    run('python3', ['-m', 'venv', VENV]);
  }
  console.log(`[${stamp()}] installing requirements`);
  run(PY, ['-m', 'pip', 'install', '-q', '--disable-pip-version-check', '-r', 'requirements.txt']);
}

// ---------------------------------------------------------------------------
// Persistence: commit botdata.json back to the repo (private repos only)
// ---------------------------------------------------------------------------
async function checkVisibility() {
  if (visibilityChecked) return repoIsPrivate;
  visibilityChecked = true;
  const repo = process.env.GITHUB_REPOSITORY;
  const token = process.env.GITHUB_TOKEN;
  if (!repo || !token) {
    console.log(`[${stamp()}] no GITHUB_TOKEN/GITHUB_REPOSITORY — cannot persist data`);
    return false;
  }
  try {
    const res = await fetch(`https://api.github.com/repos/${repo}`, {
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'User-Agent': 'synergy-bot',
      },
    });
    const data = await res.json();
    repoIsPrivate = data.private === true;
    console.log(
      `[${stamp()}] repo visibility: ${repoIsPrivate ? 'private (data will persist)' : 'PUBLIC (data will NOT be committed)'}`
    );
  } catch (e) {
    console.log(`[${stamp()}] visibility check failed: ${e.message}`);
  }
  return repoIsPrivate;
}

function git(args) {
  const res = spawnSync('git', args, { encoding: 'utf8' });
  return { ok: res.status === 0, out: `${res.stdout || ''}${res.stderr || ''}`.trim() };
}

// One dated copy per day, so git history becomes a 14-day backup set
// instead of only the latest state.
const BACKUP_DIR = 'backups';
const BACKUP_KEEP = 14;

function writeDailySnapshot() {
  try {
    if (!fs.existsSync('botdata.json')) return '';
    const today = new Date().toISOString().slice(0, 10);
    fs.mkdirSync(BACKUP_DIR, { recursive: true });
    const target = `${BACKUP_DIR}/botdata-${today}.json`;
    if (!fs.existsSync(target)) {
      fs.copyFileSync('botdata.json', target);
      console.log(`[${new Date().toISOString()}] daily snapshot written: ${target}`);
    }
    const snapshots = fs.readdirSync(BACKUP_DIR)
      .filter((f) => /^botdata-\d{4}-\d{2}-\d{2}\.json$/.test(f))
      .sort();
    const excess = snapshots.length - BACKUP_KEEP;
    for (const f of snapshots.slice(0, Math.max(0, excess))) {
      fs.unlinkSync(`${BACKUP_DIR}/${f}`);
      console.log(`[${new Date().toISOString()}] pruned old snapshot: ${f}`);
    }
    return target;
  } catch (e) {
    console.log(`snapshot failed: ${e.message}`);
    return '';
  }
}

async function commitData() {
  if (!(await checkVisibility())) {
    if (!committedOnce) {
      console.log('');
      console.log('  NOTE: warnings, giveaway entries and ticket history will reset');
      console.log('  between runs because the repo is public and committing them');
      console.log('  would expose them. Make the repo private to keep them.');
      console.log('');
      committedOnce = true;
    }
    return;
  }

  git(['config', 'user.name', 'synergy-bot']);
  git(['config', 'user.email', 'actions@users.noreply.github.com']);
  writeDailySnapshot();
  git(['add', '-A', 'botdata.json', 'transcripts', BACKUP_DIR]);

  const staged = git(['diff', '--cached', '--quiet']);
  if (staged.ok) {
    console.log(`[${stamp()}] no data changes to commit`);
    return;
  }

  git(['commit', '-q', '-m', 'chore: save bot data [skip ci]']);
  git(['pull', '--rebase', '--autostash']);
  const pushed = git(['push']);
  console.log(`[${stamp()}] data ${pushed.ok ? 'committed and pushed' : 'commit failed to push'}`);
  if (!pushed.ok) console.log(pushed.out.split('\n').slice(-3).join('\n'));
}

// ---------------------------------------------------------------------------
// Bot process
// ---------------------------------------------------------------------------
function runBot(sliceMs) {
  return new Promise((resolve) => {
    const startedSlice = Date.now();
    console.log(`[${stamp()}] starting bot (slice of ${sliceMs / 60000} min)`);
    child = spawn(PY, ['-u', 'bot.py'], { stdio: 'inherit', env: process.env });

    let settled = false;
    const finish = (why) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child = null;
      resolve({ why, durationMs: Date.now() - startedSlice });
    };

    const timer = setTimeout(() => {
      if (!child) return;
      console.log(`[${stamp()}] slice done — SIGTERM so the bot saves its data`);
      try { child.kill('SIGTERM'); } catch (e) { /* ignore */ }
      setTimeout(() => {
        if (child) { try { child.kill('SIGKILL'); } catch (e) { /* ignore */ } }
      }, 30000);
    }, sliceMs);

    child.on('exit', (code, signal) => {
      console.log(`[${stamp()}] bot exited code=${code} signal=${signal}`);
      finish('exit');
    });
    child.on('error', (e) => {
      console.error(`[${stamp()}] could not start bot: ${e.message}`);
      finish('error');
    });
  });
}

function shutdown(signal) {
  if (stopping) return;
  stopping = true;
  console.log(`[${stamp()}] ${signal} received — forwarding SIGTERM to the bot`);
  if (child) { try { child.kill('SIGTERM'); } catch (e) { /* ignore */ } }
  setTimeout(async () => {
    await commitData();
    process.exit(0);
  }, 8000);
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
(async () => {
  console.log(`[${stamp()}] synergy launcher starting`);
  try {
    ensureDependencies();
  } catch (e) {
    console.error(`[${stamp()}] dependency setup failed: ${e.message}`);
    process.exit(1);
  }
  await checkVisibility();

  // The checkout already contains the last committed botdata.json
  if (fs.existsSync('botdata.json')) {
    console.log(`[${stamp()}] found botdata.json (${fs.statSync('botdata.json').size} bytes) in the checkout`);
  }

  let quickFailures = 0;

  while (!stopping && Date.now() - startedAt < MAX_RUNTIME_MS) {
    const { durationMs } = await runBot(SLICE_MS);
    if (stopping) break;

    quickFailures = durationMs < 45000 ? quickFailures + 1 : 0;
    if (quickFailures >= 3) {
      console.error('');
      console.error('The bot exited immediately 3 times in a row. Most likely causes:');
      console.error('  * DISCORD_TOKEN is wrong or was reset');
      console.error('  * Message Content Intent is off (Developer Portal -> Bot -> Privileged Intents)');
      console.error('  * discord.py failed to install');
      console.error('');
      await commitData();
      process.exit(1);
    }

    await commitData();

    if (Date.now() - startedAt >= MAX_RUNTIME_MS) break;
    console.log(`[${stamp()}] restarting bot in 5s (${elapsedMin()}m elapsed)`);
    await new Promise((r) => setTimeout(r, 5000));
  }

  await commitData();
  console.log(`[${stamp()}] reached the 5h45m limit at ${elapsedMin()}m — next scheduled run continues`);
  process.exit(0);
})();
