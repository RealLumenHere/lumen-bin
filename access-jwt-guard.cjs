// wq#2768: origin-side Cloudflare Access JWT check for one supergateway port.
// The loopback preload (wq#2751) keeps the port off the tailnet; this closes what is left: any local
// process reaching 127.0.0.1:<port> directly, and any tunnel request that skipped Access.
// Every request to ACCESS_GUARD_PORT must carry a Cf-Access-Jwt-Assertion signed by the team's
// current Access key, iss = the team domain, aud = one of the pinned app AUDs, not expired.
// GET on ACCESS_GUARD_OPEN_PATHS (default /health) stays open for local liveness probes.
// Load with NODE_OPTIONS=--require=<this file>. Bad config = exit 78 at startup (fail closed).
// ACCESS_GUARD_MODE=shadow logs verdicts but lets requests through (rollout step only).
// ACCESS_GUARD_MODE_FILE=<path> (optional, wq#3103): re-read at most once a second, holds 'shadow' or 'enforce', so the
// mode flips without restarting the process (Commander restarts drop every session). Missing file = ACCESS_GUARD_MODE;
// unreadable or any other content = enforce (fail closed). Keep the file root-owned.
// Logs one JSON line per verdict to stderr; never logs the token or any header value.
'use strict';
const http = require('http');
const fs = require('fs');
const https = require('https');
const crypto = require('crypto');

function die(msg) { process.stderr.write(`access-guard: ${msg}, refusing to start\n`); process.exit(78); }

const PORT = Number(process.env.ACCESS_GUARD_PORT);
const TEAM = process.env.ACCESS_GUARD_TEAM || '';
const AUDS = (process.env.ACCESS_GUARD_AUD || '').split(',').map(s => s.trim()).filter(Boolean);
const MODE = process.env.ACCESS_GUARD_MODE || 'enforce';
const OPEN = (process.env.ACCESS_GUARD_OPEN_PATHS || '/health').split(',').map(s => s.trim()).filter(Boolean);
if (!/^[0-9]{1,5}$/.test(process.env.ACCESS_GUARD_PORT || '') || PORT < 1 || PORT > 65535) die('ACCESS_GUARD_PORT is not a port');
if (!/^[a-z0-9-]+$/.test(TEAM)) die('ACCESS_GUARD_TEAM missing or malformed');
if (AUDS.length === 0 || !AUDS.every(a => /^[a-f0-9]{64}$/.test(a))) die('ACCESS_GUARD_AUD missing or malformed');
if (!['enforce', 'shadow'].includes(MODE)) die('ACCESS_GUARD_MODE must be enforce or shadow');
const MODE_FILE = process.env.ACCESS_GUARD_MODE_FILE || '';
let modeAt = 0, modeCache = MODE;
function currentMode() {
  if (!MODE_FILE) return MODE;
  const now = Date.now();
  if (now - modeAt < 1000) return modeCache;
  modeAt = now;
  try {
    const v = fs.readFileSync(MODE_FILE, 'utf8').trim();
    modeCache = (v === 'shadow' || v === 'enforce') ? v : 'enforce';
  } catch (e) { modeCache = e && e.code === 'ENOENT' ? MODE : 'enforce'; }
  return modeCache;
}
const ISS = process.env.ACCESS_GUARD_ISS || `https://${TEAM}.cloudflareaccess.com`;
const CERTS_URL = process.env.ACCESS_GUARD_CERTS_URL || `${ISS}/cdn-cgi/access/certs`;
const LEEWAY = 30;            // seconds of clock skew allowed on exp/nbf
// an unknown kid triggers at most one certs fetch per window (default 30s; tests shorten it, floor 1s)
const REFETCH_MIN_MS = Math.max(1000, Number(process.env.ACCESS_GUARD_REFETCH_MS) || 30000);

let keys = new Map();
let lastFetch = 0;
let inflight = null;

function log(o) { process.stderr.write(JSON.stringify({ guard: 'access', port: PORT, mode: currentMode(), ts: new Date().toISOString(), ...o }) + '\n'); }

function b64url(s) { return Buffer.from(s.replace(/-/g, '+').replace(/_/g, '/'), 'base64'); }

function get(url) {
  const mod = url.startsWith('https:') ? https : http;
  return new Promise((resolve, reject) => {
    const r = mod.get(url, { timeout: 5000 }, res => {
      if (res.statusCode !== 200) { res.resume(); reject(new Error(`certs http ${res.statusCode}`)); return; }
      let body = ''; res.setEncoding('utf8');
      res.on('data', c => { body += c; if (body.length > 1e6) r.destroy(new Error('certs too large')); });
      res.on('end', () => resolve(body));
    });
    r.on('timeout', () => r.destroy(new Error('certs timeout')));
    r.on('error', reject);
  });
}

function fetchCerts() {
  if (inflight) return inflight;
  lastFetch = Date.now();
  inflight = get(CERTS_URL).then(body => {
    const next = new Map();
    for (const jwk of JSON.parse(body).keys || []) {
      if (jwk.kty === 'RSA' && jwk.kid) next.set(jwk.kid, crypto.createPublicKey({ key: jwk, format: 'jwk' }));
    }
    if (next.size === 0) throw new Error('certs had no RSA keys');
    keys = next;
    log({ event: 'certs', kids: [...next.keys()].map(k => k.slice(0, 12)) });
  }).catch(e => { log({ event: 'certs_error', reason: String(e.message || e) }); throw e; })
    .finally(() => { inflight = null; });
  return inflight;
}

async function keyFor(kid) {
  if (keys.has(kid)) return keys.get(kid);
  if (Date.now() - lastFetch >= REFETCH_MIN_MS || keys.size === 0) {
    try { await fetchCerts(); } catch (_) { return null; }
  }
  return keys.get(kid) || undefined;
}

async function check(token) {
  if (typeof token !== 'string' || token.length === 0) return { ok: false, code: 403, reason: 'missing' };
  const parts = token.split('.');
  if (parts.length !== 3) return { ok: false, code: 403, reason: 'malformed' };
  let header, payload;
  try { header = JSON.parse(b64url(parts[0])); payload = JSON.parse(b64url(parts[1])); }
  catch (_) { return { ok: false, code: 403, reason: 'malformed' }; }
  if (header.alg !== 'RS256' || typeof header.kid !== 'string') return { ok: false, code: 403, reason: 'bad_alg' };
  const key = await keyFor(header.kid);
  if (key === null) return { ok: false, code: 503, reason: 'certs_unavailable' };
  if (!key) return { ok: false, code: 403, reason: 'unknown_kid' };
  const good = crypto.verify('RSA-SHA256', Buffer.from(`${parts[0]}.${parts[1]}`), key, b64url(parts[2]));
  if (!good) return { ok: false, code: 403, reason: 'bad_signature' };
  const now = Math.floor(Date.now() / 1000);
  if (payload.iss !== ISS) return { ok: false, code: 403, reason: 'bad_iss' };
  const aud = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
  if (!aud.some(a => AUDS.includes(a))) return { ok: false, code: 403, reason: 'bad_aud' };
  if (typeof payload.exp !== 'number' || payload.exp < now - LEEWAY) return { ok: false, code: 403, reason: 'expired' };
  if (typeof payload.nbf === 'number' && payload.nbf > now + LEEWAY) return { ok: false, code: 403, reason: 'not_yet_valid' };
  const who = payload.email ? 'user' : (payload.common_name ? 'service_token' : 'unknown');
  return { ok: true, code: 200, reason: 'valid', who };
}

const origEmit = http.Server.prototype.emit;
http.Server.prototype.emit = function (ev, req, res) {
  if (ev !== 'request') return origEmit.apply(this, arguments);
  const addr = typeof this.address === 'function' ? this.address() : null;
  if (!addr || addr.port !== PORT) return origEmit.apply(this, arguments);
  const path = String(req.url || '').split('?')[0];
  if (req.method === 'GET' && OPEN.includes(path)) return origEmit.apply(this, arguments);
  const self = this, args = arguments;
  const h = req.headers['cf-access-jwt-assertion'];
  check(Array.isArray(h) ? h[0] : h).then(v => v, e => ({ ok: false, code: 503, reason: 'guard_error:' + (e && e.message) }))
    .then(v => {
      // via = whether cloudflared forwarded it (cf-ray present) or it came straight to the loopback port
      log({ event: 'verdict', ok: v.ok, reason: v.reason, who: v.who, via: req.headers['cf-ray'] ? 'tunnel' : 'local', method: req.method, path });
      if (v.ok || currentMode() === 'shadow') { origEmit.apply(self, args); return; }
      if (!res.headersSent) res.writeHead(v.code, { 'content-type': 'text/plain' });
      res.end(v.code === 503 ? 'access guard: signing keys unavailable\n' : 'forbidden\n');
    });
  return true;
};

fetchCerts().catch(() => {});
