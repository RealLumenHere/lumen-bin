#!/usr/bin/env bash
# mcp-gateway-watchdog — liveness probe + auto-restart (wq#2606).
# CLASS FIX vs mcp-pressure-guard.sh: that guard only trips on memory PSI or
# gateway cgroup size. A hung gateway with an open port (kill -STOP, deadlock,
# event-loop wedge) is invisible to it — the port answers TCP, nothing else
# moves. This probes actual responsiveness instead of a resource proxy:
#   mode=mcp:         POST /mcp  JSON-RPC tools/list  (MCP_CONTROL_PLANE_HA_DESIGN.md §3.3)
#   mode=cloudflared:  GET  /ready (requires --metrics <host:port> on the unit)
# 2 consecutive failures -> systemctl restart <unit>, cooldown between
# restarts of the same unit. If restarts already hit max-restarts inside the
# flap window, a further restart is refused (flapping): alert once, do not
# thrash the unit. Keeps mcp-pressure-guard.sh in place for the memory branch
# — this is the responsiveness branch, deliberately separate (different
# trigger, different failure mode).
#
# Node-agnostic by design: unit name and port are args, not baked in, so the
# same script + timer ship unmodified to Anubis/Bes/Nephthys where the unit
# is dc-gateway instead of desktop-commander-mcp (§3.2).
set -u

MODE=""
UNIT=""
PORT=""
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
  echo "       [--cooldown S] [--restart-window S] [--max-restarts N] [--fail-threshold N]" >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE=${2:-}; shift 2 ;;
    --unit) UNIT=${2:-}; shift 2 ;;
    --port) PORT=${2:-}; shift 2 ;;
    --cooldown) COOLDOWN=${2:-}; shift 2 ;;
    --restart-window) RESTART_WINDOW=${2:-}; shift 2 ;;
    --max-restarts) MAX_RESTARTS=${2:-}; shift 2 ;;
    --fail-threshold) FAIL_THRESHOLD=${2:-}; shift 2 ;;
    -h|--help) usage ;;
    *) echo "mcp-gateway-watchdog: unknown arg: $1" >&2; usage ;;
  esac
done

case "$MODE" in mcp|cloudflared) ;; *) echo "mcp-gateway-watchdog: --mode must be 'mcp' or 'cloudflared'" >&2; usage ;; esac
[ -n "$UNIT" ] || usage
case "$PORT" in ''|*[!0-9]*) echo "mcp-gateway-watchdog: --port must be numeric" >&2; usage ;; esac

mkdir -p "$STATE_DIR" 2>/dev/null
FAILS_FILE="$STATE_DIR/${UNIT}.fails"
RESTARTS_FILE="$STATE_DIR/${UNIT}.restarts"
FLAP_STAMP="$STATE_DIR/${UNIT}.flap-alerted"

read_count() {
  local f="$1"
  if [ -f "$f" ] && grep -Eq '^[0-9]+$' "$f" 2>/dev/null; then cat "$f"; else echo 0; fi
}

probe_mcp() {
  local out http_code
  out=$(curl -s -m "$CURL_TIMEOUT" -o - -w '\nHTTPCODE:%{http_code}' \
    -X POST "http://127.0.0.1:${PORT}/mcp" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":"watchdog","method":"tools/list","params":{}}' 2>/dev/null)
  http_code=$(printf '%s' "$out" | grep -o 'HTTPCODE:[0-9]*$' | cut -d: -f2)
  [ "$http_code" = "200" ] && printf '%s' "$out" | grep -q '"result"'
}

probe_cloudflared() {
  local http_code
  http_code=$(curl -s -m "$CURL_TIMEOUT" -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/ready" 2>/dev/null)
  [ "$http_code" = "200" ]
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
if [ "$MODE" = mcp ]; then probe_mcp || ok=0; else probe_cloudflared || ok=0; fi

if [ "$ok" = 1 ]; then
  echo 0 > "$FAILS_FILE"
  exit 0
fi

fails=$(read_count "$FAILS_FILE")
fails=$((fails + 1))
echo "$fails" > "$FAILS_FILE"
logger -t mcp-gateway-watchdog "probe FAILED unit=$UNIT mode=$MODE port=$PORT consecutive_fails=$fails"

[ "$fails" -lt "$FAIL_THRESHOLD" ] && exit 0

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
