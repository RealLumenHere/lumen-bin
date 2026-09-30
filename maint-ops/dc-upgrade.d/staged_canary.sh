#!/bin/bash
# staged_canary.sh <package_dir> <port> <expect_version> — run a STAGED desktop-commander build behind a throwaway
# supergateway on a spare port (same flags as the *-mcp-failover units), probe it, tear it down. Never touches a live service.
PKG="$1"; PORT="$2"; WANT="$3"; PROBE="${4:-$HOME/.dc-upgrade/dc_mcp_probe.py}"
SG=$(command -v supergateway || echo "$HOME/.npm-global/bin/supergateway")
LOG=$(mktemp /tmp/dcupg-canary.XXXXXX.log)
[ -x "$SG" ] || { echo "canary: supergateway not found"; exit 3; }
[ -f "$PKG/dist/index.js" ] || { echo "canary: $PKG/dist/index.js missing"; exit 3; }
setsid "$SG" --stdio "node $PKG/dist/index.js" --port "$PORT" --outputTransport streamableHttp --streamableHttpPath /mcp --cors --healthEndpoint /health --sessionTimeout 60000 >"$LOG" 2>&1 &
GP=$!
ok=1
for i in $(seq 1 20); do curl -s -m 2 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q ok && { ok=0; break; }; sleep 1; done
rc=1
if [ $ok -eq 0 ]; then python3 "$PROBE" "$PORT" --expect-version "$WANT"; rc=$?; else echo "canary: gateway never healthy on :$PORT"; tail -5 "$LOG"; fi
kill -TERM -- -"$GP" 2>/dev/null || kill "$GP" 2>/dev/null; sleep 1; kill -KILL -- -"$GP" 2>/dev/null
pkill -f "$PKG/dist/index.js" 2>/dev/null
rm -f "$LOG"
exit $rc
