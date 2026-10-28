// Synergy presence keeper — runs inside a GitHub Actions job.
//
// A Discord bot shows "online" only while it holds a gateway websocket.
// GitHub Actions jobs are capped at 6 hours, so this script holds the connection
// for just under 5h45m and then exits cleanly; the next scheduled run takes over.
// Expect a short grey gap (a few minutes) between runs — that is the trade for
// running on a free host.
const { Client, GatewayIntentBits, ActivityType } = require('discord.js');

const TOKEN = process.env.DISCORD_TOKEN;
if (!TOKEN) {
  console.error('Missing DISCORD_TOKEN secret.');
  console.error('Repo -> Settings -> Secrets and variables -> Actions -> New repository secret.');
  console.error('Name must be exactly DISCORD_TOKEN, and it must be under Secrets (not Variables).');
  process.exit(1);
}

// Stop before GitHub's 6h job ceiling so the run ends on our terms, not a kill.
const MAX_RUNTIME_MS = 5 * 60 * 60 * 1000 + 45 * 60 * 1000; // 5h45m
const startedAt = Date.now();

const client = new Client({ intents: [GatewayIntentBits.Guilds] });
let exiting = false;

function shutdown(reason) {
  if (exiting) return;
  exiting = true;
  console.log(`[${new Date().toISOString()}] shutting down: ${reason}`);
  try {
    client.destroy();
  } catch (e) {
    /* ignore */
  }
  process.exit(0);
}

client.once('clientReady', () => {
  console.log(`[${new Date().toISOString()}] logged in as ${client.user.tag}`);
  console.log(
    `[${new Date().toISOString()}] servers: ${client.guilds.cache.map((g) => g.name).join(', ') || 'none'}`
  );
  client.user.setPresence({
    status: 'online',
    activities: [{ name: 'channel requests in #welcome', type: ActivityType.Watching }],
  });
  console.log(`[${new Date().toISOString()}] presence online`);
});

client.on('error', (e) => console.error('gateway error:', e.message));
client.on('shardReconnecting', () => console.log('reconnecting…'));

// GitHub sends SIGTERM when it cancels or times out; leave politely.
process.on('SIGTERM', () => shutdown('SIGTERM from runner'));
process.on('SIGINT', () => shutdown('SIGINT'));

// Hold the connection, logging a heartbeat so the job never looks dead.
const beat = setInterval(() => {
  const mins = Math.round((Date.now() - startedAt) / 60000);
  console.log(`[${new Date().toISOString()}] alive ${mins}m — status ${client.isReady() ? 'online' : 'not ready'}`);
}, 15 * 60 * 1000);

setTimeout(() => {
  clearInterval(beat);
  shutdown('reached the 5h45m limit — next scheduled run continues');
}, MAX_RUNTIME_MS);

client.login(TOKEN).catch((e) => {
  const msg = String(e && e.message ? e.message : e);
  console.error('login failed:', msg);
  if (/invalid token|401|unauthorized/i.test(msg)) {
    console.error('');
    console.error('The token is wrong or was reset. Developer Portal -> your app -> Bot -> Reset Token,');
    console.error('then update BOTH the GitHub secret DISCORD_TOKEN and the platform secret SYNERGY.');
  } else if (/disallowed intents|privileged/i.test(msg)) {
    console.error('');
    console.error('Turn on Message Content: Developer Portal -> Bot -> Privileged Gateway Intents.');
  } else if (/ENOTFOUND|EAI_AGAIN|network|ETIMEDOUT/i.test(msg)) {
    console.error('');
    console.error('Network problem reaching Discord — usually transient. Re-run the workflow.');
  } else {
    console.error('');
    console.error('Check the token value has no leading/trailing spaces or newline.');
  }
  process.exit(1);
});
