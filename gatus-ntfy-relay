#!/usr/bin/env python3
"""gatus-ntfy-relay — loopback-only shim between Gatus's `custom` alert
provider (HTTP webhook only) and the house /home/lumen/bin/ntfy-send wrapper
(wq#1184).

Gatus has a built-in `ntfy` alert provider, but it talks to an ntfy server
directly and bypasses ntfy-send's priority routing and primary+OOB fan-out
(wq#901). This relay is the bridge: Gatus POSTs its templated alert body
here, this process shells out to ntfy-send, so every fleet alert — Gatus
included — goes through the one house notification path.

Deliberately dependency-free (stdlib only), matching the rest of this repo's
small systemd-run scripts (dashboard.py, dispatch_health.py).
"""
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NTFY_BIN = "/home/lumen/bin/ntfy-send"
BIND = "127.0.0.1"
PORT = 8784

# Endpoints whose failure pages Frank immediately vs. ones that can wait for
# the next dashboard glance. Keep in sync with config.yaml's endpoint names.
URGENT_ENDPOINTS = {"litellm-readiness", "litellm-health", "foreman-heartbeat"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_POST(self):
        if self.path != "/gatus-alert":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self.end_headers()
            return

        endpoint = payload.get("endpoint", "unknown")
        description = payload.get("description", "")
        status = payload.get("status", "TRIGGERED")  # "TRIGGERED" or "RESOLVED"
        priority = "urgent" if endpoint in URGENT_ENDPOINTS else "high"
        title = "Gatus %s: %s" % (status, endpoint)
        msg = description or ("%s (%s)" % (endpoint, status))

        try:
            subprocess.run(
                [NTFY_BIN, "-p", priority, "-T", title, msg],
                capture_output=True, text=True, timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            sys.stderr.write("ntfy-send failed: %s\n" % exc)
            # Best-effort by contract (matches notify_sinks.py): a failed
            # relay must not make Gatus itself think alerting is broken.

        self.send_response(200)
        self.end_headers()


def main():
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print("[gatus-ntfy-relay] serving on %s:%d" % (BIND, PORT), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
