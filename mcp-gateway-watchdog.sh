#!/usr/bin/env bash
# mcp-gateway-watchdog — liveness probe + auto-restart (wq#2606, wq#2745).
# CLASS FIX vs mcp-pressure-guard.sh: that guard only trips on memory PSI or
# gateway cgroup size. A hung gateway with an open port (kill -STOP, deadlock,
# event-loop wedge) is invisible to it — the port answers TCP, nothing else
# moves. This probes actual responsiveness instead of a resource proxy:
#   mode=mcp:         POST /mcp  full JSON-RPC `initialize` handshake
#                      (MCP_CONTROL_PLANE_HA_DESIGN.md §3.3) against the local
#                      unit's own port.
#   mode=cloudflared:  GET  /ready (requires --metrics <host:port> on the unit)
#   mode=mcpw:         POST /mcp  the same `initialize` handshake, but against
#                      mcpw (Cloudflare-fronted OAuth gateway, see
#                      /tmp/mcpw_diag.py), reusing a pre-provisioned bearer
#                      token from a 0600 file (--token-file). wq#2745: the
#                      2026-09-25 mcpw outage went undetected because the old
#                      probe only checked local DC process liveness (green)
#                      while real client `initialize` calls through mcpw
#                      failed -- this mode is what closes that gap.
# 2 consecutive failures -> systemctl restart <unit>, cooldown between
# restarts of the same unit. If restarts already hit max-restarts inside the
# flap window, a further restart is refused (flapping): alert once, do not
# thrash the unit. Keeps mcp-pressure-guard.sh in place for the memory branch
# — this is the responsiveness branch, deliberately separate (different
# trigger, different failure mode).
#
# mode=mcpw NEVER restarts anything (wq#2745: "alert, do not auto-touch
# mcpw" -- mcpw is a shared Cloudflare-fronted gateway, not a local systemd
# unit this host owns; restarting it from here would be scope creep and
# would also require credentials this sandbox must not mint). On threshold
# failures it alerts every time (no cooldown/flap suppression either --
# there is nothing to protect from being thrashed) and returns.
#
# Node-agnostic by design: unit name and port are args, not baked in, so the
# same script + timer ship unmodified to Anubis/Bes/Nephthys where the unit
# is dc-gateway instead of desktop-commander-mcp (§3.2).
set -u

MODE=""
UNIT=""
PORT=""
MCPW_URL=${WATCHDOG_MCPW_URL:-https://mcpw.lumennode.uk/mcp}
MCPW_TOKEN_FILE=${WATCHDOG_MCPW_TOKEN_FILE:-}
COOLDOWN=${WATCHDOG_COOLDOWN:-300}                 # seconds between restarts of the same unit
RESTART_WINDOW=${WATCHDOG_RESTART_WINDOW:-1800}     # flap window, seconds (30 min)
MAX_RESTARTS=${WATCHDOG_MAX_RESTARTS:-2}            # restarts allowed inside window before "flapping"
FAIL_THRESHOLD=${WATCHDOG_FAIL_THRESHOLD:-2}        # consecutive probe fails before a restart is considered
CURL_TIMEOUT=${WATCHDOG_CURL_TIMEOUT:-10}
STATE_DIR=${WATCHDOG_STATE_DIR:-/run/mcp-gateway-watchdog}
NTFY_URL=${WATCHDOG_NTFY_URL:-http://127.0.0.1:8080/lumen-fleet}
NTFY_BIN=${WATCHDOG_NTFY_BIN:-/home/lumen/bin/ntfy-send}
AQ_BIN=${WATCHDOG_AQ_BIN:-/home/lumen/bin/aq}
RESTART_CMD=${WATCHDOG_RESTART_CMD:-systemctl restart}   # override in tests: a no-op logger, never the real unit
HOST=$(hostname -s 2>/dev/null || echo unknown)

usage() {
  echo "usage: mcp-gateway-watchdog.sh --mode mcp|cloudflared --unit <systemd-unit> --port <port>" >&2
  echo "       mcp-gateway-watchdog.sh --mode mcpw --unit <label> [--mcpw-url <url>] --token-file <path>" >&2
  echo "       [--cooldown S] [--restart-window S] [--max-restarts N] [--fail-threshold N]" >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE=${2:-}; shift 2 ;;
    --unit) UNIT=${2:-}; shift 2 ;;
    --port) PORT=${2:-}; shift 2 ;;
    --mcpw-url) MCPW_URL=${2:-}; shift 2 ;;
    --token-file) MCPW_TOKEN_FILE=${2:-}; shift 2 ;;
    --cooldown) COOLDOWN=${2:-}; shift 2 ;;
    --restart-window) RESTART_WINDOW=${2:-}; shift 2 ;;
    --max-restarts) MAX_RESTARTS=${2:-}; shift 2 ;;
    --fail-threshold) FAIL_THRESHOLD=${2:-}; shift 2 ;;
    -h|--help) usage ;;
    *) echo "mcp-gateway-watchdog: unknown arg: $1" >&2; usage ;;
  esac
done

case "$MODE" in mcp|cloudflared|mcpw) ;; *) echo "mcp-gateway-watchdog: --mode must be 'mcp', 'cloudflared', or 'mcpw'" >&2; usage ;; esac
[ -n "$UNIT" ] || usage
if [ "$MODE" = mcpw ]; then
  [ -n "$MCPW_TOKEN_FILE" ] || { echo "mcp-gateway-watchdog: --mode mcpw requires --token-file" >&2; usage; }
else
  case "$PORT" in ''|*[!0-9]*) echo "mcp-gateway-watchdog: --port must be numeric" >&2; usage ;; esac
fi

mkdir -p "$STATE_DIR" 2>/dev/null
FAILS_FILE="$STATE_DIR/${UNIT}.fails"
RESTARTS_FILE="$STATE_DIR/${UNIT}.restarts"
FLAP_STAMP="$STATE_DIR/${UNIT}.flap-alerted"

read_count() {
  local f="$1"
  if [ -f "$f" ] && grep -Eq '^[0-9]+$' "$f" 2>/dev/null; then cat "$f"; else echo 0; fi
}

# Full MCP `initialize` handshake (wq#2745), not tools/list: a backend can
# keep answering tools/list fine while the actual client-facing handshake --
# protocolVersion negotiation, capabilities exchange -- is what's broken (this
# is what the 2026-09-25 mcpw outage looked like from the client side; see
# /tmp/mcpw_diag.py step 4). MCP_INIT_PAYLOAD matches that diag script's own
# `init` dict verbatim so the probe and the manual diag tool agree on shape.
MCP_INIT_PAYLOAD='{"jsonrpc":"2.0","id":"watchdog-init","method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"mcp-gateway-watchdog","version":"1"}}}'

probe_mcp() {
  local out http_code
  out=$(curl -s -m "$CURL_TIMEOUT" -o - -w '\nHTTPCODE:%{http_code}' \
    -X POST "http://127.0.0.1:${PORT}/mcp" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$MCP_INIT_PAYLOAD" 2>/dev/null)
  http_code=$(printf '%s' "$out" | grep -o 'HTTPCODE:[0-9]*$' | cut -d: -f2)
  if [ "$http_code" = "200" ] && printf '%s' "$out" | grep -q '"result"'; then
    return 0
  fi
  # wq#3103 Access JWT guard (enforce mode) answers 403 to every unauthenticated
  # request on the unit's own port except GET /health. A 403 therefore proves the
  # HTTP layer is up but says nothing about the unit; without this fallback the
  # probe fails forever and the watchdog restart-loops a healthy gateway. Fall
  # back to the one path the guard exempts. The client-facing handshake stays
  # covered by --mode mcpw (alert-only).
  if [ "$http_code" = "403" ]; then
    local hc
    hc=$(curl -s -m "$CURL_TIMEOUT" -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/health" 2>/dev/null)
    [ "$hc" = "200" ] && return 0
  fi
  return 1
}

probe_cloudflared() {
  local http_code
  http_code=$(curl -s -m "$CURL_TIMEOUT" -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/ready" 2>/dev/null)
  [ "$http_code" = "200" ]
}

# Same handshake, but through mcpw's public OAuth-fronted endpoint instead of
# the local unit's own port -- this is the path the outage actually broke.
# Token is read from a 0600 file, never a remote URL or an env var visible to
# `ps` (instinct 4).
probe_mcpw() {
  local out http_code token
  [ -r "$MCPW_TOKEN_FILE" ] || return 1
  token=$(cat "$MCPW_TOKEN_FILE" 2>/dev/null)
  [ -n "$token" ] || return 1
  out=$(curl -s -m "$CURL_TIMEOUT" -o - -w '\nHTTPCODE:%{http_code}' \
    -X POST "$MCPW_URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H "Authorization: Bearer ${token}" \
    -d "$MCP_INIT_PAYLOAD" 2>/dev/null)
  http_code=$(printf '%s' "$out" | grep -o 'HTTPCODE:[0-9]*$' | cut -d: -f2)
  [ "$http_code" = "200" ] && printf '%s' "$out" | grep -q '"result"'
}

notify() {
  local title="$1" msg="$2"
  [ -x "$NTFY_BIN" ] && "$NTFY_BIN" -p high -T "$title" "$msg" >/dev/null 2>&1
  curl -sf -m 5 -d "${title}: ${msg}" "$NTFY_URL" >/dev/null 2>&1
  [ -x "$AQ_BIN" ] && "$AQ_BIN" add mcp-gateway-watchdog "${HOST}-${UNIT}-flapping" "$title" "$msg" >/dev/null 2>&1
  logger -t mcp-gateway-watchdog "ALERT: $title -- $msg"
  return 0
}

# --- probe ---
ok=1
case "$MODE" in
  mcp) probe_mcp || ok=0 ;;
  cloudflared) probe_cloudflared || ok=0 ;;
  mcpw) probe_mcpw || ok=0 ;;
esac

if [ "$ok" = 1 ]; then
  echo 0 > "$FAILS_FILE"
  exit 0
fi

fails=$(read_count "$FAILS_FILE")
fails=$((fails + 1))
echo "$fails" > "$FAILS_FILE"
logger -t mcp-gateway-watchdog "probe FAILED unit=$UNIT mode=$MODE port=$PORT consecutive_fails=$fails"

[ "$fails" -lt "$FAIL_THRESHOLD" ] && exit 0

# mcpw is never auto-restarted (wq#2745: "alert, do not auto-touch mcpw" --
# it's a shared gateway this host doesn't own). Alert every threshold hit and
# stop; no cooldown/flap bookkeeping needed since there is no restart to
# throttle.
if [ "$MODE" = mcpw ]; then
  notify "mcpw initialize failed" "unit=$UNIT host=$HOST mcpw_url=$MCPW_URL consecutive_fails=$fails -- mcpw is not auto-restarted, needs a human look"
  logger -t mcp-gateway-watchdog "mcpw probe failed unit=$UNIT consecutive_fails=$fails -- alerted, NOT restarting (mcpw is not ours to touch)"
  exit 0
fi

# --- threshold reached: decide restart vs flap vs cooldown ---
now=$(date +%s)
if [ -f "$RESTARTS_FILE" ]; then
  awk -v now="$now" -v win="$RESTART_WINDOW" '($0+0)>0 && (now-$0)<win' "$RESTARTS_FILE" > "${RESTARTS_FILE}.tmp" 2>/dev/null
  mv "${RESTARTS_FILE}.tmp" "$RESTARTS_FILE"
fi
restarts_in_window=0
[ -f "$RESTARTS_FILE" ] && restarts_in_window=$(grep -c . "$RESTARTS_FILE" 2>/dev/null || echo 0)

if [ "${restarts_in_window:-0}" -ge "$MAX_RESTARTS" ]; then
  last_alert=$(read_count "$FLAP_STAMP")
  if [ $((now - last_alert)) -ge "$RESTART_WINDOW" ]; then
    notify "mcp-gateway flapping" "unit=$UNIT mode=$MODE host=$HOST already restarted ${restarts_in_window}x in ${RESTART_WINDOW}s -- refusing further auto-restart, needs a human look"
    echo "$now" > "$FLAP_STAMP"
  fi
  logger -t mcp-gateway-watchdog "FLAPPING unit=$UNIT restarts_in_window=$restarts_in_window >= max=$MAX_RESTARTS -- NOT restarting"
  echo 0 > "$FAILS_FILE"
  exit 0
fi

last_restart=0
[ -f "$RESTARTS_FILE" ] && last_restart=$(tail -n1 "$RESTARTS_FILE" 2>/dev/null || echo 0)
case "$last_restart" in ''|*[!0-9]*) last_restart=0 ;; esac
if [ $((now - last_restart)) -lt "$COOLDOWN" ]; then
  logger -t mcp-gateway-watchdog "restart of $UNIT suppressed -- cooldown ($((now-last_restart))s < ${COOLDOWN}s)"
  exit 0
fi

logger -t mcp-gateway-watchdog "RESTARTING unit=$UNIT mode=$MODE port=$PORT after $fails consecutive fails"
$RESTART_CMD "$UNIT"
echo "$now" >> "$RESTARTS_FILE"
echo 0 > "$FAILS_FILE"
exit 0
