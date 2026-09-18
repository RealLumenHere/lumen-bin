#!/usr/bin/env bash
# p1-status-check.sh — read-only status feed for the external station-p1 / station-p1-1
# fleet monitors (wq: p1-external-monitors, 2026-09-18).
#
# Invoked ONLY via SSH forced-command from each P1's authorized_keys entry
# (no-pty, no-forwarding — see /home/lumen/.ssh/authorized_keys). Deliberately
# does not accept arguments and does not touch anything: it is the single
# narrow window an external, less-trusted box gets into Lumen, so it stays
# read-only and dependency-free (no python here — keep it bash+coreutils so
# it can never be the thing that's broken when everything else is).
set -u

DISK_PCT=$(df / --output=pcent 2>/dev/null | tail -1 | tr -dc '0-9')
[ -z "$DISK_PCT" ] && DISK_PCT=-1

CLAUDE_BIN="/home/lumen/.npm-global/bin/claude"
RESOLVED=$(readlink -f "$CLAUDE_BIN" 2>/dev/null)
BIN_SIZE=0
[ -n "$RESOLVED" ] && [ -f "$RESOLVED" ] && BIN_SIZE=$(stat -c%s "$RESOLVED" 2>/dev/null || echo 0)
# real binary is ~220MB; the 2026-09-18 broken stub was ~500B. 1,000,000 is a
# wide, safe line between them.
BIN_OK=false
[ "$BIN_SIZE" -gt 1000000 ] 2>/dev/null && BIN_OK=true

AUTH_JSON=$(PATH=/home/lumen/.npm-global/bin:$PATH timeout 10 claude auth status 2>/dev/null)
LOGGEDIN=unknown
if echo "$AUTH_JSON" | grep -q '"loggedIn"[[:space:]]*:[[:space:]]*true'; then
  LOGGEDIN=true
elif echo "$AUTH_JSON" | grep -q '"loggedIn"'; then
  LOGGEDIN=false
fi

# mission-control (:8136) is bound to 127.0.0.1 only (verified 2026-09-18,
# left as-is — not this script's call to rebind a production service), so an
# external host can never reach it directly. Probe it from here instead.
MC_CODE=$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8136 2>/dev/null)
[ "$MC_CODE" = "200" ] && MC_OK=true || MC_OK=false

echo "disk_pct=$DISK_PCT claude_loggedin=$LOGGEDIN claude_bin_ok=$BIN_OK claude_bin_size=$BIN_SIZE mc_ok=$MC_OK mc_code=$MC_CODE"
