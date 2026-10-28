// Intentionally disabled.
//
// This repo used to run the bot inside GitHub Actions, which caps every job at
// 6 hours and cannot persist botdata.json in a public repo. The bot is now
// hosted continuously elsewhere, so this file just exits.
//
// Running it again would open a SECOND gateway connection with the same token,
// which makes the bot flap online/offline and double-reply to commands.
//
// The full launcher is preserved as launcher.js. To go back to Actions:
//   1. copy launcher.js over presence.js
//   2. make this repo private, so botdata.json is not published
console.log(
  'GitHub Actions runner disabled — the bot is hosted elsewhere.\n' +
  'See HOSTING.md. Restore with: cp launcher.js presence.js'
);
process.exit(0);
