// wq#2751: pin a node server's listen() onto 127.0.0.1, for daemons that expose no host flag.
// supergateway 3.4.3 calls app.listen(port), which binds every interface, and tailscale0 input
// accepts everything, so the port is open to the whole tailnet and to every local container.
// Load with NODE_OPTIONS=--require=<this file> and LOOPBACK_LISTEN_PORT=<port>. Only a listen on
// that port is rewritten, so child processes that inherit NODE_OPTIONS keep their own binds.
// Fails closed: a missing or malformed LOOPBACK_LISTEN_PORT kills the process at startup.
'use strict';
const net = require('net');

const raw = process.env.LOOPBACK_LISTEN_PORT;
if (!/^[0-9]{1,5}$/.test(raw || '') || Number(raw) < 1 || Number(raw) > 65535) {
  process.stderr.write(`loopback-listen: LOOPBACK_LISTEN_PORT=${JSON.stringify(raw)} is not a port, refusing to start\n`);
  process.exit(78);
}
const PORT = Number(raw);
const HOST = '127.0.0.1';
const listen = net.Server.prototype.listen;

net.Server.prototype.listen = function (...args) {
  const first = args[0];
  if (first !== null && typeof first === 'object' && !Array.isArray(first)) {
    if (Number(first.port) === PORT && first.path === undefined) {
      args[0] = { ...first, host: HOST };
    }
  } else if ((typeof first === 'number' || typeof first === 'string') && String(first) === String(PORT)) {
    if (typeof args[1] === 'string') args[1] = HOST;
    else args.splice(1, 0, HOST);
  }
  return listen.apply(this, args);
};
