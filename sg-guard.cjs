// sg-guard.cjs (2026-07-04) — prevent one dead session from killing the whole gateway.
// Context: MCP SDK webStandardStreamableHttp.send() throws
// "No connection established for request ID: N" when a tool result arrives after
// the request's HTTP stream is gone (client disconnect, child OOM, WAN blip).
// supergateway has no rejection handler, so that single benign error crashed the
// process and dropped ALL live sessions (restart counter hit 27 on 2026-07-04).
// This guard suppresses ONLY that known-benign error; anything else stays fatal.
const BENIGN = /No connection established for request ID/;
function benign(kind, err) {
  const msg = err && (err.message || String(err));
  if (msg && BENIGN.test(msg)) {
    console.error(`[sg-guard] suppressed ${kind}: ${msg}`);
    return true;
  }
  return false;
}
process.on('uncaughtException', (err) => {
  if (!benign('uncaughtException', err)) {
    console.error('[sg-guard] fatal uncaughtException:', err);
    process.exit(1);
  }
});
process.on('unhandledRejection', (err) => {
  if (!benign('unhandledRejection', err)) {
    console.error('[sg-guard] fatal unhandledRejection:', err);
    process.exit(1);
  }
});
