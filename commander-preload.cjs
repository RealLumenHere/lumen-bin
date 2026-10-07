'use strict';
// wq#3103: single preload for the Commander unit (supergateway on :3100).
//  1. loopback-listen  -> :3100 binds 127.0.0.1 only (was *)
//  2. access-jwt-guard -> every request except GET /health needs a valid Cloudflare Access JWT
//  3. wq#3266 call window (dc-call-window.cjs) -> clamp DC process-tool timeout_ms to MAX_CALL_WINDOW_MS, and hand
//     desktop-commander this same preload with COMMANDER_PRELOAD_ROLE=dc so it spawns start_process shells detached.
//     In the dc role only the detach hook loads (no listener, no guard).
//  4. then delete NODE_OPTIONS from this process, so desktop-commander (spawned by supergateway) and every
//     node process Commander shells start do NOT inherit the preload. Without this each of them would
//     fetch the Access certs and write guard lines to stderr, and `npx`/`wrangler` output would change.
//     The dc-role env is passed to the DC spawn explicitly, and DC deletes it again before spawning shells.
// All required files must sit next to this one (installed together into /home/lumen/bin).
const path = require('path');
// The Worker aborts a tools/call at 70s (mcp-oauth-worker TOOLS_CALL_TIMEOUT_MS); DC sees ~67s of that.
const MAX_CALL_WINDOW_MS = Number(process.env.MAX_CALL_WINDOW_MS) > 0 ? Number(process.env.MAX_CALL_WINDOW_MS) : 55000;
const callWindow = require(path.join(__dirname, 'dc-call-window.cjs'));
if (process.env.COMMANDER_PRELOAD_ROLE === 'dc') {
  callWindow.installDc();
} else {
  require(path.join(__dirname, 'loopback-listen.cjs'));
  require(path.join(__dirname, 'access-jwt-guard.cjs'));
  callWindow.installGateway({ maxMs: MAX_CALL_WINDOW_MS, preload: __filename });
}
delete process.env.NODE_OPTIONS;
