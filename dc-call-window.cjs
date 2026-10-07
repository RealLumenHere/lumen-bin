'use strict';
// wq#3266: keep Commander calls inside the Worker's 70s abort and keep start_process jobs alive past the call.
// Loaded by commander-preload.cjs in two processes (RCA: foreman reports/wq3264_mcpw_commander_drops_rca.md):
//  gateway (supergateway): clamp timeout_ms on start_process / interact_with_process / read_process_output in the
//    JSON-RPC line written to desktop-commander's stdin to what is left of MAX_CALL_WINDOW_MS since the DC spawn,
//    so DC answers "still running" before the Worker aborts
//    (Mode B). Each clamp logs one WARN line to stderr (journal) and one row to ~/.dc-handles.db.
//    It also hands DC the same preload with COMMANDER_PRELOAD_ROLE=dc.
//  dc (desktop-commander): spawn start_process shells detached (setsid). supergateway reaps the request's process
//    group and session when the response ends (Mode A); a new session is spared, and handle-store reads the
//    orphaned PID back on the next read_process_output.
// Limit: a detached job's stdout is still a pipe to the DC that started it. Once that DC is reaped, the job's next
// write gets SIGPIPE, so a silent job (sleep, a job logging to a file, lumen-run) survives and a chatty one does not.
const cp = require('child_process');
const os = require('os');

const CLAMPED_TOOLS = new Set(['start_process', 'interact_with_process', 'read_process_output']);
// terminal-manager.js executeCommand sets this on both spawn paths. Nothing else DC spawns sets it.
const DC_SHELL_TERM = 'xterm-256color';

// The window is the whole call as supergateway sees it, from the DC spawn. DC cold start (~1.4s measured on real DC
// 0.2.46) and the reply path come out of it, so a clamped call answers within maxMs end to end, not maxMs + startup.
const REPLY_MARGIN_MS = 500;
const MIN_TIMEOUT_MS = 1000;
const windowLeft = (maxMs, spawnedAt) => Math.max(MIN_TIMEOUT_MS, maxMs - (Date.now() - spawnedAt) - REPLY_MARGIN_MS);

function clampLine(line, limitMs, onClamp) {
  let msg;
  try { msg = JSON.parse(line); } catch (_e) { return line; }
  const p = msg && msg.method === 'tools/call' && msg.params;
  const args = p && CLAMPED_TOOLS.has(p.name) && p.arguments;
  if (!args || typeof args.timeout_ms !== 'number' || !(args.timeout_ms > limitMs)) return line;
  const requested = args.timeout_ms;
  args.timeout_ms = limitMs;
  onClamp(p.name, requested, limitMs);
  return JSON.stringify(msg) + (line.endsWith('\n') ? '\n' : '');
}

function warnClamp(tool, requested, clamped) {
  const ts = new Date().toISOString();
  process.stderr.write(JSON.stringify({ guard: 'call-window', level: 'WARN', event: 'clamp', tool,
    requested_ms: requested, clamped_ms: clamped }) + '\n');
  let db;
  try {  // fail-open: a locked or missing db never blocks the call
    const { DatabaseSync } = require('node:sqlite');
    db = new DatabaseSync(process.env.DC_HANDLE_DB || `${os.homedir()}/.dc-handles.db`);
    db.exec('PRAGMA busy_timeout=200; CREATE TABLE IF NOT EXISTS call_window_warn(id INTEGER PRIMARY KEY AUTOINCREMENT,'
      + ' ts TEXT NOT NULL, host TEXT NOT NULL, tool TEXT NOT NULL, requested_ms INTEGER, clamped_ms INTEGER, line TEXT);');
    db.prepare('INSERT INTO call_window_warn(ts,host,tool,requested_ms,clamped_ms,line) VALUES(?,?,?,?,?,?)').run(
      ts, os.hostname(), tool, requested, clamped, `WARN call-window: ${tool} timeout_ms ${requested} clamped to ${clamped}`);
  } catch (e) {
    process.stderr.write(JSON.stringify({ guard: 'call-window', level: 'WARN', event: 'db-error', error: e && e.message }) + '\n');
  } finally {
    try { if (db) db.close(); } catch (_e) { }
  }
}

function stdioCmdFromArgv(argv) {
  const i = argv.indexOf('--stdio');
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : null;
}

function installGateway({ maxMs, preload }) {
  const stdioCmd = stdioCmdFromArgv(process.argv);
  if (!stdioCmd) return;
  const origSpawn = cp.spawn;
  cp.spawn = function spawn(command, args, options) {
    // supergateway (stateless): spawn(stdioCmd, { shell: true, detached: true }), one DC per request
    const opts = !Array.isArray(args) && args && typeof args === 'object' ? args : options;
    if (command !== stdioCmd || !opts || !opts.detached) return origSpawn.apply(this, arguments);
    const env = { ...(opts.env || process.env), NODE_OPTIONS: `--require=${preload}`, COMMANDER_PRELOAD_ROLE: 'dc' };
    const spawnedAt = Date.now();
    const child = opts === args ? origSpawn.call(this, command, { ...opts, env })
      : origSpawn.call(this, command, args, { ...opts, env });
    if (child.stdin) {
      const write = child.stdin.write;
      child.stdin.write = function (chunk, ...rest) {
        if (typeof chunk === 'string') chunk = clampLine(chunk, windowLeft(maxMs, spawnedAt), warnClamp);
        return write.call(this, chunk, ...rest);
      };
    }
    return child;
  };
}

function installDc() {
  delete process.env.COMMANDER_PRELOAD_ROLE;
  if (process.platform === 'win32') return;
  const origSpawn = cp.spawn;
  cp.spawn = function spawn(command, args, options) {
    const opts = !Array.isArray(args) && args && typeof args === 'object' ? args : options;
    if (!opts || !opts.env || opts.env.TERM !== DC_SHELL_TERM || 'detached' in opts) return origSpawn.apply(this, arguments);
    return opts === args ? origSpawn.call(this, command, { ...opts, detached: true })
      : origSpawn.call(this, command, args, { ...opts, detached: true });
  };
}

module.exports = { installGateway, installDc };
