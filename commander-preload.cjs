'use strict';
// wq#3103: single preload for the Commander unit (supergateway on :3100).
//  1. loopback-listen  -> :3100 binds 127.0.0.1 only (was *)
//  2. access-jwt-guard -> every request except GET /health needs a valid Cloudflare Access JWT
//  3. then delete NODE_OPTIONS from this process, so desktop-commander (spawned by supergateway) and every
//     node process Commander shells start do NOT inherit the preload. Without this each of them would
//     fetch the Access certs and write guard lines to stderr, and `npx`/`wrangler` output would change.
// Both required files must sit next to this one (installed together into /home/lumen/bin).
const path = require('path');
require(path.join(__dirname, 'loopback-listen.cjs'));
require(path.join(__dirname, 'access-jwt-guard.cjs'));
delete process.env.NODE_OPTIONS;
